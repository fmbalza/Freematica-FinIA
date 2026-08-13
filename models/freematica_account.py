# -*- coding: utf-8 -*-
import logging
import time
from datetime import timedelta

from odoo import api, fields, models

from ..services import freematica_client as client
from ..services import freematica_matching as matching

_logger = logging.getLogger(__name__)

# Confirmado 2026-08-13: /pcon/v2/cuentas rechaza con HTTP 400 si items > 100
# ("Valor máximo permitido 100"), a diferencia de /pgrl/v2/proveedores que
# acepta hasta 2000. Con ~5470 cuentas son ~55 páginas por sync.
_SYNC_PAGE_SIZE = 100

# Presupuesto de tiempo por corrida, no número fijo de páginas: confirmado
# en producción (2026-08-13) que intentar las ~55 páginas en una sola
# corrida supera limit_time_real (120s por defecto en Odoo) y el worker
# HTTP la mata sin haber hecho commit, perdiendo todo el trabajo. Con un
# presupuesto de tiempo, cada corrida (botón o cron) avanza lo que la
# latencia real de red permita y guarda un cursor para retomar en la
# siguiente — se adapta sola a que un día la API esté más lenta que otro,
# a diferencia de un número fijo de páginas por corrida.
_SYNC_TIME_BUDGET_SECONDS = 90


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
    def sync_from_freematica(self, config=None, force=False):
        """Reanudable: procesa páginas hasta agotar el presupuesto de
        tiempo de esta corrida, guarda en qué página se quedó
        (`accounts_sync_next_page`) y retoma ahí la próxima vez (botón o
        cron) — nunca intenta las ~55 páginas completas de una sola
        corrida. Al terminar una pasada completa, el cursor vuelve a 1 y
        no se arranca otra hasta pasadas `accounts_sync_interval_hours`
        (salvo `force=True`, que además ignora ese intervalo).

        Además: índice de existentes cargado en un solo `search()`, altas
        en un único `create()` por lote, y solo se escribe un existente si
        algo realmente cambió — evita las ~11.000 queries secuenciales que
        antes hacían perder todo el trabajo al superar `limit_time_real`."""
        config = config or self.env['freematica.config'].get_active_config()

        if config.accounts_sync_next_page <= 1 and not force and config.accounts_last_sync_at:
            interval = timedelta(hours=config.accounts_sync_interval_hours or 24)
            if fields.Datetime.now() - config.accounts_last_sync_at < interval:
                return 0

        client_config = config._as_client_config()
        token = config._ensure_valid_token()
        now = fields.Datetime.now()
        deadline = time.monotonic() + _SYNC_TIME_BUDGET_SECONDS

        existing_by_key = {(r.cod_plan, r.cod_cta): r for r in self.search([])}

        page = config.accounts_sync_next_page or 1
        total = None
        count_this_run = 0
        finished = False
        try:
            while time.monotonic() < deadline:
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
                    finished = True
                    break

                to_create = []
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
                    }
                    existing = existing_by_key.get((cod_plan, cod_cta))
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
                        existing_by_key[(record.cod_plan, record.cod_cta)] = record

                page_was_short = len(items) < _SYNC_PAGE_SIZE
                page_reached_total = total is not None and page * _SYNC_PAGE_SIZE >= total
                page += 1
                self.env.cr.commit()

                if page_was_short or page_reached_total:
                    finished = True
                    break
        except client.FreematicaError as error:
            config.write({'accounts_last_sync_error': error.mensaje})
            self.env.cr.commit()
            raise

        count_so_far = (config.accounts_sync_count_so_far or 0) + count_this_run
        if finished:
            config.write({
                'accounts_sync_next_page': 1,
                'accounts_sync_count_so_far': 0,
                'accounts_last_sync_at': now,
                'accounts_last_sync_count': count_so_far,
                'accounts_last_sync_error': False,
            })
            _logger.info('Freematica: pasada completa de cuentas terminada (%d cuentas)', count_so_far)
        else:
            config.write({
                'accounts_sync_next_page': page,
                'accounts_sync_count_so_far': count_so_far,
                'accounts_last_sync_error': False,
            })
            _logger.info(
                'Freematica: sync de cuentas en progreso (%d hasta ahora), retoma en página %d',
                count_so_far, page,
            )
        self.env.cr.commit()
        return count_so_far
