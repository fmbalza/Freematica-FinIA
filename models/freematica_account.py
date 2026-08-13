# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

from ..services import freematica_client as client
from ..services import freematica_matching as matching

_logger = logging.getLogger(__name__)

# Confirmado 2026-08-13: /pcon/v2/cuentas rechaza con HTTP 400 si items > 100
# ("Valor máximo permitido 100"), a diferencia de /pgrl/v2/proveedores que
# acepta hasta 2000. Con ~5470 cuentas son ~55 páginas por sync.
_SYNC_PAGE_SIZE = 100


class FreematicaAccount(models.Model):
    _name = 'freematica.account'
    _description = 'Cuenta contable Freematica (caché de /pcon/v2/cuentas)'
    _order = 'cod_cta'
    _sql_constraints = [
        ('cod_plan_cta_unique', 'unique(cod_plan, cod_cta)',
         'Ya existe esa cuenta para ese plan contable.'),
    ]

    cod_plan = fields.Char(string='Plan (COD_PLAN)', required=True, index=True)
    cod_cta = fields.Char(string='Código (COD_CTA)', required=True, index=True)
    des_cta = fields.Char(string='Descripción')
    des_cta2 = fields.Char(string='Descripción (cont.)')
    cta_activa = fields.Boolean(string='Activa')
    subcuenta = fields.Boolean(
        string='Es subcuenta (imputable)',
        help='SUBCUENTA=1 en Freematica: cuenta de nivel hoja, apta para usarse en un '
             'asiento. Las que no lo son son cuentas de agrupación (ej. "62" sin más '
             'dígitos) y no deberían poder elegirse como BORRL_CTA.',
    )
    normalized_des = fields.Char(compute='_compute_normalized_des', store=True, index=True)
    last_synced_at = fields.Datetime(string='Última sincronización')

    @api.depends('des_cta', 'des_cta2')
    def _compute_normalized_des(self):
        for record in self:
            record.normalized_des = matching.normalize_name(
                '%s %s' % (record.des_cta or '', record.des_cta2 or '')
            )

    @api.model
    def search_for_picker(self, query, limit=20):
        """Búsqueda para el selector de cuenta en el frontend: solo cuentas
        activas e imputables (subcuenta=True), por código o por descripción
        (normalizada, sin acentos/mayúsculas)."""
        query = (query or '').strip()
        if not query:
            return self.browse()
        domain = [('cta_activa', '=', True), ('subcuenta', '=', True)]
        normalized_query = matching.normalize_name(query)
        domain += ['|', ('cod_cta', 'like', query), ('normalized_des', 'ilike', normalized_query)]
        return self.search(domain, limit=limit)

    @api.model
    def sync_from_freematica(self, config=None):
        config = config or self.env['freematica.config'].get_active_config()
        client_config = config._as_client_config()
        token = config._ensure_valid_token()
        now = fields.Datetime.now()

        page = 1
        total = None
        count = 0
        try:
            while True:
                result = client.list_cuentas(
                    client_config, page=page, items=_SYNC_PAGE_SIZE, token=token,
                )
                data = result.get('data') if isinstance(result, dict) else None
                items = (data or {}).get('items') or []
                if total is None:
                    try:
                        total = int((data or {}).get('total') or 0)
                    except (TypeError, ValueError):
                        total = None
                if not items:
                    break
                for item in items:
                    cod_plan = item.get('COD_PLAN')
                    cod_cta = item.get('COD_CTA')
                    if not cod_plan or not cod_cta:
                        continue
                    vals = {
                        'cod_plan': cod_plan,
                        'cod_cta': cod_cta,
                        'des_cta': item.get('DES_CTA'),
                        'des_cta2': item.get('DES_CTA2'),
                        'cta_activa': bool(item.get('CTA_ACTIVA')),
                        'subcuenta': bool(item.get('SUBCUENTA')),
                        'last_synced_at': now,
                    }
                    existing = self.search([('cod_plan', '=', cod_plan), ('cod_cta', '=', cod_cta)], limit=1)
                    if existing:
                        existing.write(vals)
                    else:
                        self.create(vals)
                    count += 1
                if len(items) < _SYNC_PAGE_SIZE:
                    break
                if total is not None and page * _SYNC_PAGE_SIZE >= total:
                    break
                page += 1
        except client.FreematicaError as error:
            config.write({'accounts_last_sync_error': error.mensaje})
            raise

        config.write({
            'accounts_last_sync_at': now,
            'accounts_last_sync_count': count,
            'accounts_last_sync_error': False,
        })
        _logger.info('Freematica: %s cuentas contables sincronizadas', count)
        return count
