# -*- coding: utf-8 -*-
import logging
import time
from datetime import timedelta

from odoo import api, fields, models

from ..services import freematica_client as client
from ..services import freematica_matching as matching

_logger = logging.getLogger(__name__)

_SYNC_PAGE_SIZE = 500

# Ver freematica_account.py: mismo mecanismo de presupuesto de tiempo +
# cursor reanudable, por el mismo riesgo de superar limit_time_real (menor
# aquí con solo ~4 páginas para 1828 proveedores, pero el mecanismo es
# igual de válido si el catálogo crece o la red está lenta ese día).
_SYNC_TIME_BUDGET_SECONDS = 90


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
    cta_gasto = fields.Char(
        string='Cuenta de gasto (CMP_CTA_GASTO)',
        help='Cuenta de gasto (Debe) que este proveedor tiene asignada en Freematica, si la '
             'tiene (campo opcional/personalizado: solo ~9% de los proveedores de Servinet la '
             'tienen rellena, confirmado 2026-08-13). Cuando existe, se usa para autocompletar '
             'finia.ocr.vendor.default_accounting_account al matchear — sin esto, esa cuenta '
             'sigue siendo manual, Freematica no la expone para todos.',
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
    def sync_from_freematica(self, config=None, force=False):
        """Reanudable, ver freematica_account.py::sync_from_freematica
        (mismo mecanismo: presupuesto de tiempo por corrida, cursor de
        página persistido, altas en lote, escritura solo si cambió algo,
        commit por página, y una pasada completa no se repite antes de
        `providers_sync_interval_hours` salvo `force=True`)."""
        config = config or self.env['freematica.config'].get_active_config()

        if config.providers_sync_next_page <= 1 and not force and config.providers_last_sync_at:
            interval = timedelta(hours=config.providers_sync_interval_hours or 24)
            if fields.Datetime.now() - config.providers_last_sync_at < interval:
                return 0

        client_config = config._as_client_config()
        token = config._ensure_valid_token()
        now = fields.Datetime.now()
        deadline = time.monotonic() + _SYNC_TIME_BUDGET_SECONDS

        existing_by_key = {r.cod_pro: r for r in self.search([])}

        page = config.providers_sync_next_page or 1
        total = None
        count_this_run = 0
        finished = False
        try:
            while time.monotonic() < deadline:
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
                    finished = True
                    break

                to_create = []
                for item in items:
                    cod_pro = item.get('COD_PRO')
                    if not cod_pro:
                        continue
                    vals = {
                        'cod_pro': cod_pro,
                        'nombre_pro': item.get('NOMBRE_PRO'),
                        'nif': item.get('NIF'),
                        'cta_contable': item.get('CTA_CONTABLE'),
                        'cta_gasto': item.get('CMP_CTA_GASTO'),
                        'referencia': item.get('REFERENCIA'),
                        'poblacion': item.get('COD_POBLACION'),
                        'email': item.get('E_MAIL'),
                        'telefono': item.get('TELEFONO1'),
                    }
                    existing = existing_by_key.get(cod_pro)
                    if existing:
                        changed = any(existing[field] != value for field, value in vals.items())
                        if changed:
                            existing.write(vals)
                    else:
                        to_create.append(dict(vals, last_synced_at=now))
                    count_this_run += 1

                if to_create:
                    created = self.create(to_create)
                    for record in created:
                        existing_by_key[record.cod_pro] = record

                page_was_short = len(items) < _SYNC_PAGE_SIZE
                page_reached_total = total is not None and page * _SYNC_PAGE_SIZE >= total
                page += 1
                self.env.cr.commit()

                if page_was_short or page_reached_total:
                    finished = True
                    break
        except client.FreematicaError as error:
            config.write({'providers_last_sync_error': error.mensaje})
            self.env.cr.commit()
            raise

        count_so_far = (config.providers_sync_count_so_far or 0) + count_this_run
        if finished:
            config.write({
                'providers_sync_next_page': 1,
                'providers_sync_count_so_far': 0,
                'providers_last_sync_at': now,
                'providers_last_sync_count': count_so_far,
                'providers_last_sync_error': False,
            })
            _logger.info('Freematica: pasada completa de proveedores terminada (%d proveedores)', count_so_far)
        else:
            config.write({
                'providers_sync_next_page': page,
                'providers_sync_count_so_far': count_so_far,
                'providers_last_sync_error': False,
            })
            _logger.info(
                'Freematica: sync de proveedores en progreso (%d hasta ahora), retoma en página %d',
                count_so_far, page,
            )
        self.env.cr.commit()
        return count_so_far
