# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

from ..services import freematica_matching as matching

_logger = logging.getLogger(__name__)


class FiniaOcrVendor(models.Model):
    _name = 'finia.ocr.vendor'
    _inherit = 'finia.ocr.vendor'

    nif = fields.Char(
        string='NIF/CIF',
        help='NIF/CIF del proveedor. No lo extrae el OCR hoy; se completa a mano, o se '
             'autocompleta desde el proveedor Freematica matcheado si estaba vacío. Usado '
             'como BORRL_NIF al enviar facturas de este proveedor a Freematica.',
    )
    freematica_cod_aux = fields.Char(
        string='Código auxiliar Freematica',
        help='Código auxiliar (BORRL_CODAUX) que identifica a este proveedor dentro de '
             'Freematica. Se autocompleta desde freematica_provider_id.cod_pro al '
             'matchear (por NIF o por nombre); si se edita a mano, esa edición manual '
             'nunca se pisa. Obligatorio para poder enviar sus facturas como asiento '
             'contable.',
    )
    freematica_provider_id = fields.Many2one(
        'freematica.provider', string='Proveedor Freematica', ondelete='set null', copy=False,
    )
    freematica_match_score = fields.Float(string='Score de coincidencia Freematica', readonly=True)
    freematica_match_state = fields.Selection([
        ('no_intentado', 'No intentado'),
        ('coincidencia_nif', 'Coincidencia por NIF'),
        ('coincidencia_automatica', 'Coincidencia automática (nombre)'),
        ('coincidencia_manual', 'Coincidencia manual'),
        ('sin_coincidencia', 'Sin coincidencia'),
    ], string='Estado coincidencia Freematica', default='no_intentado', copy=False)
    freematica_matched_at = fields.Datetime(string='Última resolución Freematica', readonly=True)
    freematica_has_irpf = fields.Boolean(
        string='Facturas con IRPF',
        help='Marcar si este proveedor es un profesional/autónomo sujeto a retención de '
             'IRPF (aplica a todas sus facturas, no varía factura a factura). Determina qué '
             'plantilla (BORR_PLANT) se usa al enviar sus facturas a Freematica: '
             'freematica.config.plantilla_irpf en vez de plantilla_normal. Finia no calcula '
             'ni extrae el importe de la retención hoy — esto solo decide la plantilla.',
    )

    def _freematica_resolve_provider(self, force=False):
        """Intenta matchear este proveedor Finia contra el catálogo cacheado
        de freematica.provider: primero por NIF exacto (alta confianza), y
        si no hay NIF o no matchea, por nombre con fuzzy-matching (mismo
        algoritmo que finia_facturaweb). Nunca pisa una coincidencia
        marcada como manual salvo que se pida force=True, ni un
        freematica_cod_aux/nif ya escritos a mano."""
        self.ensure_one()
        if self.freematica_match_state == 'coincidencia_manual' and self.freematica_provider_id and not force:
            return self.freematica_provider_id

        Provider = self.env['freematica.provider']
        provider = self.env['freematica.provider']
        match_state = 'sin_coincidencia'
        score = 0.0

        # `nif` es el campo de este módulo (manual o heredado de Freematica);
        # `vat` es el NIF que lee el OCR. Sin el segundo casi todo dependía
        # del parecido del nombre.
        normalized_nif = matching.normalize_nif(self.nif or self.vat)
        if normalized_nif:
            provider = Provider.search([('normalized_nif', '=', normalized_nif)], limit=1)
            if provider:
                match_state = 'coincidencia_nif'
                score = 1.0

        if not provider:
            threshold = self.env['freematica.config'].get_active_config().provider_match_threshold
            candidates = [(p.id, p.nombre_pro) for p in Provider.search([]) if p.nombre_pro]
            best_id, best_score = matching.best_match(self.name, candidates, threshold=threshold)
            score = best_score
            if best_id:
                provider = Provider.browse(best_id)
                match_state = 'coincidencia_automatica'

        vals = {
            'freematica_match_score': score,
            'freematica_matched_at': fields.Datetime.now(),
            'freematica_match_state': match_state,
            'freematica_provider_id': provider.id if provider else False,
        }
        if provider:
            if not self.freematica_cod_aux:
                vals['freematica_cod_aux'] = provider.cod_pro
            if not self.nif and provider.nif:
                vals['nif'] = provider.nif
            if not self.default_accounting_account and provider.cta_gasto:
                vals['default_accounting_account'] = provider.cta_gasto
        self.write(vals)
        return self.freematica_provider_id

    def _freematica_pending_invoices_domain(self):
        """Facturas que se van a enviar a Freematica: procesadas o
        contabilizadas, no enviadas y recibidas desde el inicio de envíos."""
        config = self.env['freematica.config'].sudo().search([('active', '=', True)], limit=1)
        domain = [('state', 'in', ('procesado', 'revisado', 'contabilizado', 'irregularidades')),
                  ('freematica_state', '!=', 'enviado')]
        if config.fecha_inicio_envio:
            since = fields.Datetime.to_datetime(config.fecha_inicio_envio)
            domain += ['|', ('reception_date', '>=', since),
                       '&', ('reception_date', '=', False), ('create_date', '>=', since)]
        return domain

    @api.model
    def _freematica_match_pending(self, retry_unmatched=True):
        """Empareja con el catálogo de Freematica los proveedores de las
        facturas pendientes de enviar que aún no lo están, para avisar ANTES
        de enviar (antes solo se intentaba al pulsar Enviar). Devuelve los
        proveedores que siguen sin coincidencia."""
        states = ('no_intentado', 'sin_coincidencia') if retry_unmatched else ('no_intentado',)
        invoices = self.env['finia.invoice'].sudo().search(self._freematica_pending_invoices_domain())
        vendors = invoices.mapped('ocr_vendor_id').filtered(
            lambda v: v.freematica_match_state in states and not v.freematica_cod_aux)
        for vendor in vendors:
            try:
                vendor._freematica_resolve_provider()
            except Exception as error:  # noqa: BLE001 - un proveedor raro no frena al resto
                _logger.warning('Freematica: no se pudo emparejar el proveedor %s: %s', vendor.id, error)
        return vendors.filtered(lambda v: not v.freematica_cod_aux)

    @api.model
    def _cron_freematica_match_pending(self):
        self._freematica_match_pending()

    @api.model
    def freematica_vendor_alerts(self):
        """Proveedores de facturas pendientes de enviar que no existen (o no
        se reconocen) en Freematica, con sus facturas: lo que muestra el panel
        de avisos del frontend."""
        invoices = self.env['finia.invoice'].sudo().search(
            self._freematica_pending_invoices_domain() + [('ocr_vendor_id', '!=', False)],
            order='reception_date desc, id desc')
        alerts = []
        for vendor in invoices.mapped('ocr_vendor_id'):
            if vendor.freematica_cod_aux:
                continue
            vendor_invoices = invoices.filtered(lambda i: i.ocr_vendor_id == vendor)
            alerts.append({
                'vendor_id': vendor.id,
                'vendor_name': vendor.name,
                'nif': vendor.nif or vendor.vat or None,
                'match_state': vendor.freematica_match_state,
                'checked_at': vendor.freematica_matched_at and fields.Datetime.to_string(vendor.freematica_matched_at),
                'invoices': [{
                    'id': inv.id,
                    'name': inv.name,
                    'invoice_number': inv.ocr_invoice_number,
                    'invoice_date': inv.ocr_invoice_date and fields.Date.to_string(inv.ocr_invoice_date),
                    'total': inv.ocr_total_amount,
                    'state': inv.state,
                } for inv in vendor_invoices],
            })
        return alerts

    def action_freematica_set_manual_match(self, provider_id):
        self.ensure_one()
        provider = self.env['freematica.provider'].browse(provider_id)
        vals = {
            'freematica_provider_id': provider.id,
            'freematica_match_state': 'coincidencia_manual',
            'freematica_match_score': 1.0,
            'freematica_matched_at': fields.Datetime.now(),
            'freematica_cod_aux': provider.cod_pro,
        }
        if not self.default_accounting_account and provider.cta_gasto:
            vals['default_accounting_account'] = provider.cta_gasto
        self.write(vals)
        return True
