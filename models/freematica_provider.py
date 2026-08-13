# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

from ..services import freematica_client as client
from ..services import freematica_matching as matching

_logger = logging.getLogger(__name__)

_SYNC_PAGE_SIZE = 500


class FreematicaProvider(models.Model):
    _name = 'freematica.provider'
    _description = 'Proveedor Freematica (caché de /pgrl/v2/proveedores)'
    _order = 'nombre_pro'
    _sql_constraints = [
        ('cod_pro_unique', 'unique(cod_pro)', 'Ya existe un proveedor Freematica con ese COD_PRO.'),
    ]

    cod_pro = fields.Char(string='COD_PRO', required=True, index=True)
    nombre_pro = fields.Char(string='Nombre')
    nif = fields.Char(string='NIF', index=True)
    cta_contable = fields.Char(
        string='Cuenta contable (CTA_CONTABLE)',
        help='Cuenta contable real de este proveedor en Freematica. Varía por proveedor '
             '(confirmado 2026-08-13: no es una única cuenta genérica para todos) — se usa '
             'como BORRL_CTA de la línea de proveedor al enviar sus facturas, con la cuenta '
             'de la configuración solo como respaldo si el proveedor no está matcheado.',
    )
    referencia = fields.Char(string='Referencia')
    poblacion = fields.Char(string='Población')
    email = fields.Char(string='Email')
    telefono = fields.Char(string='Teléfono')
    normalized_nombre = fields.Char(compute='_compute_normalized_nombre', store=True, index=True)
    normalized_nif = fields.Char(compute='_compute_normalized_nif', store=True, index=True)
    last_synced_at = fields.Datetime(string='Última sincronización')

    @api.depends('nombre_pro')
    def _compute_normalized_nombre(self):
        for record in self:
            record.normalized_nombre = matching.normalize_name(record.nombre_pro)

    @api.depends('nif')
    def _compute_normalized_nif(self):
        for record in self:
            record.normalized_nif = matching.normalize_nif(record.nif)

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
                result = client.list_proveedores(
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
                    cod_pro = item.get('COD_PRO')
                    if not cod_pro:
                        continue
                    vals = {
                        'cod_pro': cod_pro,
                        'nombre_pro': item.get('NOMBRE_PRO'),
                        'nif': item.get('NIF'),
                        'cta_contable': item.get('CTA_CONTABLE'),
                        'referencia': item.get('REFERENCIA'),
                        'poblacion': item.get('COD_POBLACION'),
                        'email': item.get('E_MAIL'),
                        'telefono': item.get('TELEFONO1'),
                        'last_synced_at': now,
                    }
                    existing = self.search([('cod_pro', '=', cod_pro)], limit=1)
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
            config.write({'providers_last_sync_error': error.mensaje})
            raise

        config.write({
            'providers_last_sync_at': now,
            'providers_last_sync_count': count,
            'providers_last_sync_error': False,
        })
        _logger.info('Freematica: %s proveedores sincronizados', count)
        return count
