# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.osv import expression


class FreematicaCatalogMixin(models.AbstractModel):
    """Catálogo cacheado de una configuración de Freematica (cuentas,
    proveedores, artículos): cada fila pertenece a la configuración (empresa de
    Freematica) que la sincronizó."""
    _name = 'freematica.catalog.mixin'
    _description = 'Catálogo Freematica por configuración'

    config_id = fields.Many2one(
        'freematica.config', string='Configuración Freematica', index=True, ondelete='cascade',
        help='Configuración (empresa de Freematica) a la que pertenece esta fila del catálogo.')

    @api.model
    def _freematica_scoped(self, config=None):
        """El modelo acotado a una configuración: todo `search`/`search_count`
        posterior solo ve su catálogo. Sin config, la de por defecto."""
        config = config or self.env['freematica.config']._current()
        return self.with_context(freematica_config_id=config.id)

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        config_id = self.env.context.get('freematica_config_id')
        if config_id:
            domain = expression.AND([[('config_id', '=', config_id)], list(domain or [])])
        return super()._search(domain, offset=offset, limit=limit, order=order)
