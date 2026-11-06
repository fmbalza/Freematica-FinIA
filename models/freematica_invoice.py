# -*- coding: utf-8 -*-
import calendar
import logging
from collections import defaultdict
from datetime import date, datetime

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .freematica_ocr_vendor import _RETRY_STATES
from .freematica_payment_term import freematica_fecha_vencimiento

_logger = logging.getLogger(__name__)
# Estados en los que una factura ya tiene proveedor leído y se enviará a Freematica.
_FREEMATICA_PENDING_STATES = ('procesado', 'revisado', 'contabilizado', 'irregularidades')

_ROUND = 2
_FREEMATICA_CPIVA_CODE = {0: '0', 4: '3', 10: '2', 21: '1'}
# Tipos de impuesto (BORRID_TIMPUES) de la tabla de Servinet (Josep, 2026-11):
# S0 = soportado interior sujeto no exento (el habitual), S4 = sujeto exento,
# S10 = sujeto no exento con retención de profesionales (IRPF). Los casos que
# la factura no permite deducir (intracomunitarias, ISP, importación...) se
# fijan por proveedor en finia.ocr.vendor.freematica_tipo_impuesto.
_TIMPUES_DEFAULT = 'S0'
_TIMPUES_EXENTO = 'S4'
_TIMPUES_IRPF = 'S10'
_FREEMATICA_DOC_SEQUENCE = 'freematica.documento.serie'
# Tipo de línea (BORRL_TIPOLIN) de la plantilla de factura de proveedor, tal
# como aparecen en los asientos reales de Servinet (export-asientos, plantilla 30).
_TIPOLIN_PROVEEDOR = '2'
_TIPOLIN_IVA = '4'
_TIPOLIN_GASTO = '21'
# Cartera de proveedor (en los asientos reales de Servinet; 1 = clientes).
_TIPOCAR_PROVEEDOR = '2'


def freematica_fecha_registro(fecha_factura, hoy):
    """Fecha de registro contable (BORR_FCHASI) según la regla del SII que
    aplica Servinet (Sonia, 2026-10): una factura de un mes anterior se
    registra con el último día de ese mes anterior al de hoy si se
    contabiliza hasta el día 15; a partir del 16, con la fecha de hoy. Una
    factura del mes en curso, con la fecha de hoy. La fecha de la factura
    viaja aparte, en BORRL_FCHDOC."""
    if (fecha_factura.year, fecha_factura.month) >= (hoy.year, hoy.month) or hoy.day > 15:
        return hoy
    anio, mes = (hoy.year, hoy.month - 1) if hoy.month > 1 else (hoy.year - 1, 12)
    return date(anio, mes, calendar.monthrange(anio, mes)[1])


class FiniaInvoice(models.Model):
    _name = 'finia.invoice'
    _inherit = ['finia.invoice', 'freematica.sendable.mixin']

    freematica_doc_number = fields.Integer(
        string='Nº documento Freematica (BORRL_DOC)', readonly=True, copy=False,
        help='Número correlativo de la serie contable, asignado por Finia la primera vez '
             'que se intenta enviar la factura y reutilizado en los reintentos.',
    )

    # ── Emparejar el proveedor con Freematica en cuanto se conoce ─────────
    # Antes solo lo hacía el cron (cada 15 min) y el panel avisaba de
    # proveedores "sin alta" que en realidad sí existían (Servinet, 03/11/2026).

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if any(vals.get('ocr_vendor_id') for vals in vals_list):
            records._freematica_match_vendors_now()
        return records

    def write(self, vals):
        result = super().write(vals)
        if vals.get('ocr_vendor_id') or vals.get('state') in _FREEMATICA_PENDING_STATES:
            self._freematica_match_vendors_now()
        return result

    def _freematica_match_vendors_now(self):
        vendors = self.mapped('ocr_vendor_id').filtered(
            lambda v: not v.freematica_cod_aux and v.freematica_match_state in _RETRY_STATES)
        for vendor in vendors:
            try:
                with self.env.cr.savepoint():
                    vendor.sudo()._freematica_resolve_provider()
            except Exception as error:  # noqa: BLE001 - nunca debe frenar el guardado de la factura
                _logger.warning('Freematica: no se pudo emparejar el proveedor %s: %s', vendor.id, error)

    @staticmethod
    def _freematica_line_rates(line):
        """Tipos de IVA de una línea. Si el OCR no le asignó impuestos pero sí
        leyó la cuota (FINIA/00307: 34,36 + 3,44 sin impuesto), se deduce el
        tipo de cuota / base cuando coincide con uno de Freematica (0, 4, 10,
        21 %); si no coincide, la línea queda sin IVA y el envío se bloquea al
        no cuadrar el total, como antes."""
        if line.tax_ids:
            return [round(tax.rate, 2) for tax in line.tax_ids]
        base = line.subtotal or 0.0
        cuota = line.tax_amount or 0.0
        if not base:
            return []
        if abs(cuota) < 0.005:
            return [0.0]
        computed = cuota / base * 100.0
        for rate in sorted(_FREEMATICA_CPIVA_CODE):
            # Tolerancia de redondeo: medio céntimo de cuota sobre la base.
            if abs(computed - rate) <= max(0.05, 0.5 / abs(base)):
                return [float(rate)]
        return []

    def _freematica_check_before_send(self):
        """Junta TODOS los problemas encontrados (en vez de fallar con el
        primero — evita el ciclo de "corrijo uno, reintento, aparece el
        siguiente") y devuelve `(problems, problem_codes)`: la lista humana
        y la lista paralela de códigos máquina, para que el frontend sepa
        exactamente qué modal/acción mostrar sin parsear texto."""
        self.ensure_one()
        problems = []
        problem_codes = []

        if self.state != 'contabilizado':
            problems.append(
                'la factura debe estar en estado "Contabilizado" (fecha contable, '
                'referencia y demás datos contables se fijan en ese paso); está en "%s"'
                % dict(self._fields['state'].selection).get(self.state, self.state)
            )
            problem_codes.append('not_contabilizado')
        if not (self.ocr_invoice_number or self.name):
            problems.append('falta el número de factura')
            problem_codes.append('missing_invoice_number')
        config = self.env['freematica.config'].sudo().search([('active', '=', True)], limit=1)
        recibida = fields.Datetime.to_datetime(self.reception_date or self.create_date)
        if config.fecha_inicio_envio and recibida and recibida.date() < config.fecha_inicio_envio:
            problems.append(
                'la factura se recibió el %s, antes del inicio de envíos a Freematica (%s): las '
                'anteriores las registra el cliente directamente en Freematica y enviarla la '
                'duplicaría' % (recibida.strftime('%d/%m/%Y'), config.fecha_inicio_envio.strftime('%d/%m/%Y'))
            )
            problem_codes.append('before_send_start_date')
        if not self.ocr_invoice_date:
            problems.append('falta la fecha de factura')
            problem_codes.append('missing_invoice_date')
        if not self.accounting_date:
            problems.append('falta la fecha contable')
            problem_codes.append('missing_accounting_date')
        if not self.line_ids:
            problems.append('la factura no tiene líneas')
            problem_codes.append('no_lines')
        if not self.ocr_total_amount:
            problems.append('falta el importe total')
            problem_codes.append('missing_total_amount')

        vendor = self.ocr_vendor_id
        if not vendor:
            problems.append('falta el proveedor (OCR)')
            problem_codes.append('missing_vendor')
        else:
            if vendor.freematica_match_state == 'no_intentado':
                vendor._freematica_resolve_provider()
            if not vendor.freematica_cod_aux and vendor.freematica_match_state == 'varios_nif':
                codes = ', '.join(vendor._freematica_nif_candidates().mapped('cod_pro'))
                problems.append(
                    'el NIF del proveedor "%s" corresponde a varias fichas de Freematica (%s): '
                    'elige cuál usar en la sección "Proveedor Freematica" de la factura'
                    % (vendor.name, codes)
                )
                problem_codes.append('vendor_ambiguous')
            elif not vendor.freematica_cod_aux:
                problems.append(
                    'el proveedor "%s" no tiene código auxiliar de Freematica: ni coincidencia '
                    'automática (sincroniza proveedores desde FinIA - Freematica > '
                    'Configuración) ni completado a mano' % vendor.name
                )
                problem_codes.append('vendor_not_matched')

        if self.line_ids:
            missing_accounts = self.line_ids.filtered(lambda l: not l.accounting_account)
            if missing_accounts:
                names = ', '.join(n for n in missing_accounts.mapped('name') if n)
                problems.append(
                    '%d línea(s) sin cuenta contable de gasto resuelta (accounting_account): '
                    '%s — asigna una categoría de gasto a la línea, o configura la cuenta por '
                    'defecto del proveedor en finia.ocr.vendor.default_accounting_account'
                    % (len(missing_accounts), names[:300])
                )
                problem_codes.append('missing_accounting_account')
            # Una cuenta del grupo 1 (capital, reservas...) nunca es de gasto: se
            # coló varias veces desde el selector (10000000 "Capital social").
            wrong_accounts = self.line_ids.filtered(
                lambda l: l.accounting_account and l.accounting_account.startswith('1'))
            if wrong_accounts:
                problems.append(
                    '%d línea(s) con una cuenta del grupo 1 (%s), que no es de gasto — '
                    'asigna una cuenta del grupo 6' % (
                        len(wrong_accounts), ', '.join(sorted(set(wrong_accounts.mapped('accounting_account')))))
                )
                problem_codes.append('missing_accounting_account')

        return problems, problem_codes

    def _freematica_after_sent(self):
        """Tras un envío exitoso, promueve finia.invoice.state a 'enviado' —
        el único camino permitido hacia ese estado (ver
        finia_invoice.py._check_state_transition en finIA_backend). Solo se
        llega aquí cuando _freematica_check_before_send ya confirmó
        state == 'contabilizado', así que el salto siempre es
        contabilizado -> enviado."""
        self.with_context(freematica_confirmed_send=True).write({'state': 'enviado'})

    def action_freematica_check(self):
        """Corre la misma validación que se ejecuta antes de enviar, pero
        sin llamar a la API — pensado para auditar una factura de antemano
        y ver TODOS los problemas de una vez, sin gastar intentos reales
        contra Freematica."""
        self.ensure_one()
        self._freematica_validate_before_send()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Freematica'),
                'message': _('"%s" pasó todas las validaciones — lista para enviar.') % self.display_name,
                'type': 'success',
                'sticky': False,
            },
        }

    def _freematica_assign_doc_number(self, config, accounting_date):
        """Nº de documento de la serie contable (BORRL_DOC/BORRC_DOC).
        Josep Segura/Servinet 2026-10: lo genera Finia (el API no expone el
        contador de Freematica), correlativo por ejercicio — sigue al último
        nº registrado en Freematica y vuelve a empezar cada año. Se asigna
        una sola vez y se guarda en la factura, para que un reintento tras
        un rechazo reutilice el mismo número en vez de dejar un hueco."""
        self.ensure_one()
        if self.freematica_doc_number:
            return self.freematica_doc_number
        if not config.contador_documento_listo:
            raise UserError(_(
                'El contador de nº de documento de Freematica no está inicializado: fija el '
                'próximo número del ejercicio en la secuencia "%s" y marca "Contador de '
                'documento listo" en la configuración de Freematica.'
            ) % _FREEMATICA_DOC_SEQUENCE)
        number = self.env['ir.sequence'].sudo().next_by_code(
            _FREEMATICA_DOC_SEQUENCE, sequence_date=accounting_date,
        )
        if not number:
            raise UserError(_('No existe la secuencia "%s".') % _FREEMATICA_DOC_SEQUENCE)
        self.write({'freematica_doc_number': int(number)})
        return self.freematica_doc_number

    def _freematica_build_asiento_payload(self, config):
        self.ensure_one()
        vendor = self.ocr_vendor_id
        fecha_factura = self.ocr_invoice_date or self.accounting_date
        hoy = datetime.now(pytz.timezone('Europe/Madrid')).date()
        fecha_registro = freematica_fecha_registro(fecha_factura, hoy)
        fecha_doc = fecha_factura.strftime('%Y%m%d')
        fecha_asiento = fecha_registro.strftime('%Y%m%d')
        currency = (self.currency or 'EUR')[:4]
        serie = (config.serie_contable_default or '')[:5]
        # Ref. asiento = nº de la factura tal como figura en el documento
        # (Sonia/Servinet 2026-10), no la referencia interna de Finia.
        reference = (self.ocr_invoice_number or self.name or '')[:40]
        calendario = str(fecha_registro.year)
        concepto = config.concepto_asiento_default or 'FACT'
        # Mismo formato que los asientos que registra Servinet a mano.
        descripcion_base = ('Su Fra. %s %s' % (reference, vendor.name))[:40]
        localizacion = vendor.freematica_provider_id._freematica_localizacion_pago(config)

        lineas = []

        # Línea Haber: proveedor (contrapartida, por el importe total de la factura).
        # Cuenta real del proveedor en Freematica (varía por proveedor, confirmado
        # 2026-08-13) si está matcheado; si no, la cuenta de respaldo de la config.
        cuenta_proveedor = vendor.freematica_provider_id.cta_contable or config.cuenta_proveedor_default
        proveedor_linea = {
            'BORRL_CTA': cuenta_proveedor,
            'BORRL_CODAUX': vendor.freematica_cod_aux,
            'BORRL_CONASI': concepto,
            'BORRL_DESCON': descripcion_base,
            'BORRL_DIV': currency,
            'BORRL_DDIV': 0,
            'BORRL_HDIV': 0,  # se fija al final con la suma del Debe
            'BORRL_TIPOLIN': _TIPOLIN_PROVEEDOR,
            # Tipo de asociación del documento (Servinet, 2026-11): 1 en la línea
            # del proveedor, como en sus asientos de factura.
            'BORRL_TASOC': '1',
            'BORRL_CAL': calendario,
            'BORRL_SER': serie,
            # Nombre de la ficha de Freematica: el OCR a veces lee como proveedor el
            # nombre del cliente (FINIA/00298: "Serveis de Personal i Neteja" en
            # una factura de ABAC con el NIF de ABAC).
            'BORRL_NOMAUX': (vendor.freematica_provider_id.nombre_pro or vendor.name)[:200],
            # NIF de la ficha de Freematica emparejada (o el escrito a mano).
            'BORRL_NIF': (vendor.nif or vendor.freematica_provider_id.nif or '')[:15],
            'BORRL_FCHDOC': fecha_doc,
            'BORRL_REF': reference,
        }
        # Grupo auxiliar de proveedores: los asientos reales de Servinet usan
        # 1; con la config en 0 no se enviaba y la Cartera no se generaba.
        grupo_aux = config.grupo_aux_proveedor or 1
        proveedor_linea['BORRL_GRUPAUX'] = grupo_aux
        if localizacion.get('fpago'):
            proveedor_linea['BORRL_FPAGO'] = localizacion['fpago']
            proveedor_linea['BORRL_LOC'] = localizacion['loc']
        # Cartera obligatoria siempre (Josep Segura/Servinet 2026-10). El
        # vencimiento: el que trae la factura; si no, la fecha de factura más
        # los días de la forma de pago del proveedor (Sonia, 2026-10-27:
        # 31/07 a 30 días vence el 31/08); sin forma de pago conocida, la
        # fecha de la factura.
        fecha_vcto = fecha_doc
        if self.ocr_due_date:
            fecha_vcto = self.ocr_due_date.strftime('%Y%m%d')
        elif localizacion.get('fpago'):
            forma_pago = self.env['freematica.payment.term'].sudo().search(
                [('code', '=', localizacion['fpago'])], limit=1)
            if forma_pago:
                fecha_vcto = freematica_fecha_vencimiento(
                    fecha_factura, forma_pago.days, config.regla_vencimiento).strftime('%Y%m%d')
        cartera_entry = {
            'BORRC_TIPOCAR': _TIPOCAR_PROVEEDOR,
            'BORRC_CALEN': calendario,
            'BORRC_SER': serie,
            'BORRC_ORD': 1,
            'BORRC_CODAUX': vendor.freematica_cod_aux,
            'BORRC_FCHDOC': fecha_doc,
            'BORRC_FCHVCTO': fecha_vcto,
            'BORRC_DIV': currency,
            'BORRC_IMPDV': 0,  # se fija al final con la suma del Debe
        }
        cartera_entry['BORRC_GRUPAUX'] = grupo_aux
        if localizacion.get('fpago'):
            cartera_entry['BORRC_FPAGO'] = localizacion['fpago']
            cartera_entry['BORRC_LOC'] = localizacion['loc']
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
                'BORRL_CONASI': concepto,
                'BORRL_DESCON': descripcion_base,
                'BORRL_DIV': currency,
                'BORRL_DDIV': round(bucket['subtotal'], _ROUND),
                'BORRL_HDIV': 0,
                'BORRL_TIPOLIN': _TIPOLIN_GASTO,
                'BORRL_CAL': calendario,
                'BORRL_SER': serie,
                'BORRL_FCHDOC': fecha_doc,
                'BORRL_REF': reference,
            })

        # Retención (IRPF): Finia la guarda en la factura (ocr_retention_*); el
        # total de la factura ya es base + IVA - retención.
        retention_type = self.ocr_retention_type
        retention = round(self.ocr_retention_amount or 0.0, _ROUND)
        if retention and retention_type != 'irpf':
            raise UserError(_(
                'La factura "%s" tiene una retención de tipo "%s": a Freematica solo se envían '
                'retenciones de IRPF. Revísala antes de enviarla.'
            ) % (self.display_name, dict(self._fields['ocr_retention_type'].selection).get(retention_type, retention_type)))
        timpues = vendor.freematica_tipo_impuesto or (_TIMPUES_IRPF if retention else _TIMPUES_DEFAULT)

        # Líneas Debe: IVA soportado, agrupadas por tipo (finia.tax.rate). Las
        # bases al 0 % van al registro de IVA como exentas (S4), sin cuota.
        by_rate = defaultdict(lambda: {'base': 0.0, 'cuota': 0.0})
        for line in self.line_ids:
            for rate in self._freematica_line_rates(line):
                bucket = by_rate[rate]
                bucket['base'] += line.subtotal
                bucket['cuota'] += line.subtotal * (rate / 100.0)
        base_total = sum(bucket['base'] for bucket in by_rate.values()) or 0.0
        iva_entries = []
        for rate, bucket in by_rate.items():
            cuota = round(bucket['cuota'], _ROUND)
            if not rate:
                if bucket['base']:
                    iva_entries.append({
                        'BORRID_TIMPUES': vendor.freematica_tipo_impuesto or _TIMPUES_EXENTO,
                        'BORRID_CPIVA': _FREEMATICA_CPIVA_CODE[0],
                        'BORRID_BIVA': round(bucket['base'], _ROUND),
                        'BORRID_PIVA': 0.0,
                        'BORRID_CUOTA': 0.0,
                        'BORRID_TOTCUOTA': 0.0,
                    })
                continue
            if not cuota:
                continue
            rate_label = str(int(rate)) if float(rate).is_integer() else str(rate)
            cpiva_code = _FREEMATICA_CPIVA_CODE.get(int(round(rate)))
            if cpiva_code is None:
                raise UserError(_(
                    'Tipo de IVA %s%% sin código Freematica configurado (BORRID_CPIVA) — '
                    'solo 0%%, 4%%, 10%%, 21%% están soportados. Factura "%s".'
                ) % (rate_label, self.display_name))
            lineas.append({
                'BORRL_CTA': config.cuenta_iva_soportado_default,
                'BORRL_CONASI': concepto,
                'BORRL_DESCON': descripcion_base,
                'BORRL_DIV': currency,
                'BORRL_DDIV': cuota,
                'BORRL_HDIV': 0,
                'BORRL_TIPOLIN': _TIPOLIN_IVA,
                'BORRL_CAL': calendario,
                'BORRL_SER': serie,
                'BORRL_FCHDOC': fecha_doc,
                'BORRL_REF': reference,
            })
            iva_entries.append({
                'BORRID_TIMPUES': timpues,
                'BORRID_CPIVA': cpiva_code,
                'BORRID_BIVA': round(bucket['base'], _ROUND),
                'BORRID_PIVA': rate,
                'BORRID_CUOTA': cuota,
                'BORRID_TOTCUOTA': cuota,
            })
        # Retención en el registro de IVA, repartida por base entre los tipos.
        if retention and iva_entries:
            percent = self.ocr_retention_percent or (base_total and round(retention / base_total * 100, 2)) or 0.0
            remaining = retention
            for index, entry in enumerate(iva_entries):
                share = remaining if index == len(iva_entries) - 1 else round(
                    retention * (entry['BORRID_BIVA'] / base_total) if base_total else 0.0, _ROUND)
                remaining = round(remaining - share, _ROUND)
                entry['BORRID_PRET'] = percent
                entry['BORRID_RETEN'] = share
        # El detalle de IVA va asociado a la línea del proveedor (junto con
        # CARTERA), no a la línea de la cuenta de IVA -- confirmado por
        # Josep Segura/Servinet 2026-09-14: Freematica grababa cabecera y
        # líneas pero no el registro de IVA con la estructura anterior.
        if iva_entries:
            proveedor_linea['IVA'] = iva_entries

        # El Haber del proveedor (y la Cartera) es la suma del Debe (bases +
        # cuotas redondeadas), no el total leído por OCR: así el asiento cuadra
        # al céntimo (el total OCR puede diferir en 0,01 por redondeo — pasó
        # en la factura 59: 465,92 vs. 465,91). Una diferencia mayor indica
        # líneas o impuestos mal leídos y no se envía.
        total_debe = round(sum(l['BORRL_DDIV'] for l in lineas), _ROUND)
        a_pagar = round(total_debe - retention, _ROUND)
        if abs(a_pagar - (self.ocr_total_amount or 0.0)) > 0.05:
            raise UserError(_(
                'La suma de bases e IVA%s (%.2f) no coincide con el total de la factura (%.2f) — '
                'no se envía a Freematica. Revisa las líneas e impuestos de la factura "%s".'
            ) % (_(' menos la retención') if retention else '', a_pagar, self.ocr_total_amount or 0.0, self.display_name))
        proveedor_linea['BORRL_HDIV'] = a_pagar
        cartera_entry['BORRC_IMPDV'] = a_pagar
        if retention:
            # Retención de IRPF: Haber a la cuenta de retenciones de profesionales.
            lineas.append({
                'BORRL_CTA': config.cuenta_irpf or '47510002',
                'BORRL_CONASI': concepto,
                'BORRL_DESCON': descripcion_base,
                'BORRL_DIV': currency,
                'BORRL_DDIV': 0,
                'BORRL_HDIV': retention,
                'BORRL_CAL': calendario,
                'BORRL_SER': serie,
                'BORRL_FCHDOC': fecha_doc,
                'BORRL_REF': reference,
            })

        # El nº de documento se asigna al final, ya validado el asiento: un
        # asiento que ni se llega a enviar no debe consumir número.
        doc_number = self._freematica_assign_doc_number(config, fecha_registro)
        for linea in lineas:
            linea['BORRL_DOC'] = doc_number
        for cartera_entry in proveedor_linea['CARTERA']:
            cartera_entry['BORRC_DOC'] = doc_number

        plantilla = config.plantilla_irpf if (vendor.freematica_has_irpf or retention) else config.plantilla_normal
        return {
            'BORR_CODEMP': config.x_auth_company,
            'BORR_COD': ('INV%07d' % self.id)[:10],
            'BORR_PLANT': plantilla or '',
            'BORR_FCHASI': fecha_asiento,
            'BORR_CALEN': calendario,
            'BORR_PER': fecha_registro.month,
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
