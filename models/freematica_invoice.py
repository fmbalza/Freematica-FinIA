# -*- coding: utf-8 -*-
import re
from collections import defaultdict

from odoo import _, models
from odoo.exceptions import UserError

_ROUND = 2


class FiniaInvoice(models.Model):
    _name = 'finia.invoice'
    _inherit = ['finia.invoice', 'freematica.sendable.mixin']

    def _freematica_validate_before_send(self):
        self.ensure_one()
        missing = []
        if not (self.ocr_invoice_number or self.name):
            missing.append('número de factura')
        if not self.ocr_invoice_date:
            missing.append('fecha de factura')
        if not self.accounting_date:
            missing.append('fecha contable')
        if not self.line_ids:
            missing.append('líneas de factura')
        if not self.ocr_total_amount:
            missing.append('importe total')
        if not self.ocr_vendor_id:
            missing.append('proveedor (OCR)')
        if missing:
            raise UserError(_(
                'Faltan datos obligatorios para enviar la factura a Freematica: %s.'
            ) % ', '.join(missing))

        vendor = self.ocr_vendor_id
        if not vendor.freematica_cod_aux:
            raise UserError(_(
                'El proveedor "%s" no tiene configurado su código auxiliar de Freematica '
                '(finia.ocr.vendor.freematica_cod_aux). Configúralo antes de enviar.'
            ) % vendor.name)

        missing_accounts = self.line_ids.filtered(lambda l: not l.accounting_account)
        if missing_accounts:
            raise UserError(_(
                'Hay líneas de factura sin cuenta contable resuelta (accounting_account): %s.'
            ) % ', '.join(n for n in missing_accounts.mapped('name') if n))

    def _freematica_doc_number(self):
        """Extrae la parte numérica de ocr_invoice_number para BORRL_DOC/
        BORRC_DOC (number(10) en el esquema); si no hay dígitos
        aprovechables, usa el id de la factura. El esquema documentado
        parece pensado para documentos de venta con numeración puramente
        numérica — confirmar con Freematica si esto es aceptable para
        facturas de proveedor con numeración alfanumérica."""
        self.ensure_one()
        digits = re.sub(r'\D', '', self.ocr_invoice_number or '')
        if digits:
            try:
                return int(digits[-10:])
            except ValueError:
                pass
        return self.id

    def _freematica_build_asiento_payload(self, config):
        self.ensure_one()
        vendor = self.ocr_vendor_id
        accounting_date = self.accounting_date or self.ocr_invoice_date
        fecha_doc = (self.ocr_invoice_date or accounting_date).strftime('%Y%m%d')
        fecha_asiento = accounting_date.strftime('%Y%m%d')
        currency = (self.currency or 'EUR')[:4]
        doc_number = self._freematica_doc_number()
        serie = (config.serie_contable_default or '')[:5]
        reference = (self.posting_reference or self.name or '')[:40]
        descripcion_base = ('Factura %s - %s' % (self.ocr_invoice_number or self.name, vendor.name))[:40]

        lineas = []

        # Línea Haber: proveedor (contrapartida, por el importe total de la factura)
        proveedor_linea = {
            'BORRL_CTA': config.cuenta_proveedor_default,
            'BORRL_CODAUX': vendor.freematica_cod_aux,
            'BORRL_CONASI': config.concepto_asiento_default or 'FACT',
            'BORRL_DESCON': descripcion_base,
            'BORRL_DIV': currency,
            'BORRL_DDIV': 0,
            'BORRL_HDIV': round(self.ocr_total_amount, _ROUND),
            'BORRL_SER': serie,
            'BORRL_DOC': doc_number,
            'BORRL_NOMAUX': vendor.name[:200],
            'BORRL_NIF': (vendor.nif or '')[:15],
            'BORRL_FCHDOC': fecha_doc,
            'BORRL_REF': reference,
        }
        if config.grupo_aux_proveedor:
            proveedor_linea['BORRL_GRUPAUX'] = config.grupo_aux_proveedor
        if self.ocr_due_date:
            cartera_entry = {
                'BORRC_TIPOCAR': '1',
                'BORRC_DOC': doc_number,
                'BORRC_ORD': 1,
                'BORRC_CODAUX': vendor.freematica_cod_aux,
                'BORRC_FCHDOC': fecha_doc,
                'BORRC_FCHVCTO': self.ocr_due_date.strftime('%Y%m%d'),
                'BORRC_DIV': currency,
                'BORRC_IMPDV': round(self.ocr_total_amount, _ROUND),
            }
            if config.grupo_aux_proveedor:
                cartera_entry['BORRC_GRUPAUX'] = config.grupo_aux_proveedor
            proveedor_linea['CARTERA'] = [cartera_entry]
        lineas.append(proveedor_linea)

        # Líneas Debe: gasto/compra, agrupadas por cuenta contable ya
        # resuelta en cada línea (finia.invoice.line.accounting_account,
        # vía finia.config.account.rule) — no se recalcula ningún mapeo aquí.
        by_account = defaultdict(lambda: {'subtotal': 0.0, 'names': []})
        for line in self.line_ids:
            bucket = by_account[line.accounting_account]
            bucket['subtotal'] += line.subtotal
            if line.name or line.product_code:
                bucket['names'].append(line.name or line.product_code)
        for account, bucket in by_account.items():
            lineas.append({
                'BORRL_CTA': account,
                'BORRL_CONASI': config.concepto_asiento_default or 'FACT',
                'BORRL_DESCON': (', '.join(bucket['names']) or descripcion_base)[:40],
                'BORRL_DIV': currency,
                'BORRL_DDIV': round(bucket['subtotal'], _ROUND),
                'BORRL_HDIV': 0,
                'BORRL_SER': serie,
                'BORRL_DOC': doc_number,
                'BORRL_FCHDOC': fecha_doc,
                'BORRL_REF': reference,
            })

        # Líneas Debe: IVA soportado, agrupadas por tipo (finia.tax.rate)
        by_rate = defaultdict(lambda: {'base': 0.0, 'cuota': 0.0})
        for line in self.line_ids:
            for tax in line.tax_ids:
                rate = round(tax.rate, 2)
                if not rate:
                    continue
                bucket = by_rate[rate]
                bucket['base'] += line.subtotal
                bucket['cuota'] += line.subtotal * (tax.rate / 100.0)
        for rate, bucket in by_rate.items():
            cuota = round(bucket['cuota'], _ROUND)
            if not cuota:
                continue
            rate_label = str(int(rate)) if float(rate).is_integer() else str(rate)
            lineas.append({
                'BORRL_CTA': config.cuenta_iva_soportado_default,
                'BORRL_CONASI': config.concepto_asiento_default or 'FACT',
                'BORRL_DESCON': ('IVA soportado %s%%' % rate_label)[:40],
                'BORRL_DIV': currency,
                'BORRL_DDIV': cuota,
                'BORRL_HDIV': 0,
                'BORRL_SER': serie,
                'BORRL_DOC': doc_number,
                'BORRL_FCHDOC': fecha_doc,
                'BORRL_REF': reference,
                'IVA': [{
                    'BORRID_TIMPUES': 'IVA',
                    'BORRID_CPIVA': rate_label,
                    'BORRID_BIVA': round(bucket['base'], _ROUND),
                    'BORRID_PIVA': rate,
                    'BORRID_CUOTA': cuota,
                    'BORRID_TOTCUOTA': cuota,
                }],
            })

        total_debe = round(sum(l['BORRL_DDIV'] for l in lineas), _ROUND)
        total_haber = round(sum(l['BORRL_HDIV'] for l in lineas), _ROUND)
        if abs(total_debe - total_haber) > 0.01:
            raise UserError(_(
                'El asiento no está balanceado (Debe %.2f vs. Haber %.2f) — no se envía a '
                'Freematica. Revisa las cuentas/importes de la factura "%s".'
            ) % (total_debe, total_haber, self.display_name))

        return {
            'BORR_CODEMP': config.x_auth_company,
            'BORR_COD': ('INV%07d' % self.id)[:10],
            'BORR_PLANT': '',
            'BORR_FCHASI': fecha_asiento,
            'BORR_CALEN': str(accounting_date.year),
            'BORR_PER': accounting_date.month,
            'BORR_DIARIO': config.diario_compras,
            'BORR_REF': reference,
            'BORR_CODDIV': currency,
            'LINEAS': lineas,
        }

    def action_send_to_freematica(self):
        """Override del placeholder de finIA_backend
        (finia_invoice.py:action_send_to_freematica): envía de verdad la(s)
        factura(s) a Freematica como asiento contable. Devuelve una
        notificación `ir.actions.client`, el mismo contrato que el
        placeholder que reemplaza, para no romper el botón ya existente en
        el formulario ni el endpoint HTTP que lo llama."""
        results = [record._freematica_send_one() for record in self]
        if len(results) == 1:
            result = results[0]
            message = result.get('message') or result.get('error') or ''
            notif_type = 'success' if result.get('success') else 'danger'
        else:
            success_count = sum(1 for result in results if result.get('success'))
            message = _('%d de %d facturas enviadas a Freematica correctamente.') % (
                success_count, len(results),
            )
            notif_type = 'success' if success_count == len(results) else 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Freematica'),
                'message': message,
                'type': notif_type,
                'sticky': False,
            },
        }
