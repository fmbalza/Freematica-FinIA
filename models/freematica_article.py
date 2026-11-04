# -*- coding: utf-8 -*-
"""Caché del catálogo de artículos de Freematica (/part/v1/articulos).

Sirve para avisar en Finia, antes de enviar un albarán de compra, de los
artículos que no existen en Freematica: el alta del albarán entra igual,
pero falla al facturarlo (Josep, 04/11/2026). También da el código de IVA
de cada artículo cuando el Excel del proveedor no lo trae."""
import logging

from odoo import api, fields, models

from ..services import freematica_client as client

_logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_PAGES = 200


class FreematicaArticle(models.Model):
    _name = 'freematica.article'
    _description = 'Artículo Freematica (caché de /part/v1/articulos)'
    _order = 'cod_articulo'
    _rec_name = 'cod_articulo'
    _sql_constraints = [
        ('cod_articulo_unique', 'unique(cod_articulo)', 'Ya existe un artículo Freematica con ese código.'),
    ]

    cod_articulo = fields.Char(string='Código', required=True, index=True)
    descripcion = fields.Char(string='Descripción')
    cod_proveedor = fields.Char(string='Proveedor (COD_PRO)', index=True)
    cod_iva = fields.Char(string='Código IVA')
    cod_familia = fields.Char(string='Familia')
    id_reg = fields.Char(string='idReg')
    last_synced_at = fields.Datetime(string='Última sincronización')

    @api.model
    def sync_from_freematica(self, config):
        """Descarga el catálogo completo (unos cientos de artículos) y lo
        guarda. Los artículos que ya no vienen se borran de la caché."""
        token = config._ensure_valid_token()
        client_config = config._as_client_config()
        now = fields.Datetime.now()
        seen = {}
        for page in range(1, _MAX_PAGES + 1):
            response = client.list_articulos(client_config, page=page, items=_PAGE_SIZE, token=token)
            data = response.get('data') if isinstance(response, dict) else None
            items = (data or {}).get('items') or []
            for item in items:
                code = (item.get('COD_ARTICULO') or '').strip()
                if code:
                    seen[code] = {
                        'descripcion': item.get('DESC_ART') or False,
                        'cod_proveedor': (item.get('COD_PROVEEDOR') or '').strip() or False,
                        'cod_iva': (item.get('COD_IVA') or '').strip() or False,
                        'cod_familia': (item.get('COD_FAMILIA') or '').strip() or False,
                        'id_reg': item.get('idReg') or False,
                        'last_synced_at': now,
                    }
            if len(items) < _PAGE_SIZE:
                break
        existing = {a.cod_articulo: a for a in self.search([])}
        for code, vals in seen.items():
            if code in existing:
                existing[code].write(vals)
            else:
                self.create(dict(vals, cod_articulo=code))
        stale = self.browse([a.id for code, a in existing.items() if code not in seen])
        if seen and stale:
            stale.unlink()
        config.sudo().write({'articles_last_sync_at': now})
        _logger.info('Freematica: %d artículos sincronizados', len(seen))
        return len(seen)
