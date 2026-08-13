# -*- coding: utf-8 -*-
from odoo import fields, models

from ..services import freematica_matching as matching


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

        normalized_nif = matching.normalize_nif(self.nif)
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
        self.write(vals)
        return self.freematica_provider_id

    def action_freematica_set_manual_match(self, provider_id):
        self.ensure_one()
        provider = self.env['freematica.provider'].browse(provider_id)
        self.write({
            'freematica_provider_id': provider.id,
            'freematica_match_state': 'coincidencia_manual',
            'freematica_match_score': 1.0,
            'freematica_matched_at': fields.Datetime.now(),
            'freematica_cod_aux': provider.cod_pro,
        })
        return True
