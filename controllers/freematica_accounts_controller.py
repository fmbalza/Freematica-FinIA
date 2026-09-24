# -*- coding: utf-8 -*-
"""Endpoints propios (no overrides) para el flujo de asignación manual de
cuenta contable: buscar en el plan de cuentas real de Freematica, listar
facturas pendientes de asignación, y guardar lo que el usuario elija —
sin disparar el envío real a Freematica (eso sigue siendo un paso aparte,
explícito, desde `send-to-freematica`).

También expone el catálogo completo de cuentas (para la pantalla de
administración de "Cuenta Contable" del frontend) y el reparto de un
movimiento bancario sin factura entre una o varias cuentas contables reales,
cada una con su propio importe — ver freematica_bank_movement_account_line.py.
"""
import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.finIA_backend.controllers.cors_utils import json_response, cors_preflight_response
from ..services import freematica_matching as matching

_logger = logging.getLogger(__name__)

# Nota: todas las rutas de este controller responden SIEMPRE HTTP 200,
# incluso cuando success=False. El cliente HTTP de Finia (api.service.ts)
# trata cualquier status no-2xx como excepción y descarta el cuerpo de la
# respuesta sin leerlo — si se devolviera 400/404/500, el frontend nunca
# vería `error` y solo mostraría un mensaje genérico de catch. Mismo
# patrón que ya usa email_controller.py::connect_email_imap en
# finIA_backend.


def _read_json_body():
    try:
        return json.loads(request.httprequest.data.decode('utf-8')) if request.httprequest.data else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _invoice_pending_dict(invoice):
    return {
        'id': invoice.id,
        'name': invoice.display_name,
        'ocr_invoice_number': invoice.ocr_invoice_number,
        'ocr_total_amount': invoice.ocr_total_amount,
        'partner': {
            'id': invoice.ocr_vendor_id.id,
            'name': invoice.ocr_vendor_id.name,
            'default_accounting_account': invoice.ocr_vendor_id.default_accounting_account,
        } if invoice.ocr_vendor_id else None,
        'lines': [{
            'id': line.id,
            'name': line.name,
            'accounting_account': line.accounting_account,
        } for line in invoice.line_ids],
    }


def _account_dict(record):
    return {
        'id': record.id,
        'cod_plan': record.cod_plan,
        'cod_cta': record.cod_cta,
        'des_cta': record.des_cta,
        'des_cta2': record.des_cta2,
        'cta_activa': record.cta_activa,
        'subcuenta': record.subcuenta,
        'last_synced_at': record.last_synced_at,
    }


def _account_line_dict(line):
    return {
        'id': line.id,
        'movement_id': line.movement_id.id,
        'account': _account_dict(line.freematica_account_id),
        'amount': line.amount,
    }


def _movement_lines_summary(movement):
    return {
        'movement_id': movement.id,
        'category': movement.category,
        'reconciliation_state': movement.reconciliation_state,
        'lines': [_account_line_dict(l) for l in movement.freematica_account_line_ids],
    }


def _recompute_movement_from_lines(movement):
    """Recalcula `category` (resumen legible, para vistas que no cargan las
    líneas reales) y `reconciliation_state` a partir de las líneas de cuenta
    contable del movimiento — mismo criterio que
    bank_integration_controller.py::_recompute_reconciliation_state para
    facturas: 'descartado' (resuelto, sin factura) solo cuando la suma de
    líneas cubre el importe total; si no, sigue 'pendiente' aunque ya tenga
    1+ líneas, para que el frontend pueda seguir repartiendo el resto."""
    lines = movement.freematica_account_line_ids
    if lines:
        labels = ['%s (%.2f)' % (l.freematica_account_id.cod_cta, l.amount) for l in lines]
        category = 'Cuenta contable: ' + ', '.join(labels)
    else:
        category = False
    total_assigned = sum(lines.mapped('amount'))
    reconciliation_state = (
        'descartado' if lines and total_assigned >= abs(movement.amount) - 0.01 else 'pendiente'
    )
    movement.write({'category': category, 'reconciliation_state': reconciliation_state})


class FreematicaAccountsController(http.Controller):

    @http.route([
        '/api/v1/freematica/cuentas',
        '/api/v1/invoices/pending-accounting-account',
        '/api/v1/invoices/<int:invoice_id>/assign-accounting-account',
    ], type='http', auth='public', methods=['OPTIONS'], csrf=False)
    def preflight(self, **kwargs):
        return cors_preflight_response()

    @http.route('/api/v1/freematica/cuentas', type='http', auth='public', methods=['GET'], csrf=False)
    def search_cuentas(self, **kwargs):
        """GET /api/v1/freematica/cuentas?search=<texto>&limit=20
        Busca en el plan de cuentas cacheado (freematica.account) — solo
        cuentas activas e imputables del plan real de Servinet (PGCS) — para
        el selector del frontend."""
        query = request.params.get('search', '')
        try:
            limit = int(request.params.get('limit') or 20)
        except (TypeError, ValueError):
            limit = 20
        try:
            accounts = request.env['freematica.account'].sudo().search_for_picker(query, limit=limit)
            return json_response({
                'success': True,
                'data': [{
                    'cod_plan': a.cod_plan,
                    'cod_cta': a.cod_cta,
                    'des_cta': (a.des_cta or '') + (a.des_cta2 or ''),
                } for a in accounts],
            })
        except Exception as error:
            _logger.error('Freematica search cuentas error: %s', error)
            return json_response({'success': False, 'error': str(error)}, 200)

    # ── Catálogo completo de cuentas (pantalla de administración) ──────────
    # Distinto de `search_cuentas` (arriba): ese es el selector compacto para
    # elegir UNA cuenta en un modal; este es el listado paginado completo,
    # con creación/edición, para la pantalla de Configuración Contable.

    @http.route('/api/v1/freematica/cuentas/catalog', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def list_cuentas_catalog(self, **kwargs):
        """GET /api/v1/freematica/cuentas/catalog?search=&cod_plan=PGCS&limit=50&offset=0
        Listado paginado del catálogo completo (no solo el top-N del picker),
        para la pantalla de administración de cuentas contables. Por defecto
        filtra por `cod_plan=PGCS` (el plan real de Servinet — ver
        freematica_account.py: PGCD/PGCS/PYM comparten códigos con
        significados distintos, no mezclar)."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        query = (request.params.get('search') or '').strip()
        cod_plan = request.params.get('cod_plan') or 'PGCS'
        try:
            limit = int(request.params.get('limit') or 50)
            offset = int(request.params.get('offset') or 0)
        except (TypeError, ValueError):
            return json_response({'success': False, 'error': 'Parámetros de paginación inválidos'}, 200)

        domain = [('cod_plan', '=', cod_plan)]
        if query:
            normalized_query = matching.normalize_name(query)
            domain += ['|', ('cod_cta', 'like', query), ('normalized_des', 'ilike', normalized_query)]

        try:
            Account = request.env['freematica.account'].sudo()
            total = Account.search_count(domain)
            accounts = Account.search(domain, limit=limit, offset=offset, order='cod_cta')
            return json_response({
                'success': True,
                'data': [_account_dict(a) for a in accounts],
                'total': total,
                'limit': limit,
                'offset': offset,
            })
        except Exception as error:
            _logger.error('Freematica list cuentas catalog error: %s', error)
            return json_response({'success': False, 'error': str(error)}, 200)

    @http.route('/api/v1/freematica/cuentas', type='http', auth='public', methods=['POST'], csrf=False)
    def create_cuenta(self, **kwargs):
        """POST /api/v1/freematica/cuentas
        Crea una cuenta contable manualmente (fuera de la sincronización
        automática con Freematica). Body: {cod_cta, des_cta, des_cta2?,
        cod_plan? (default 'PGCS'), cta_activa? (default true), subcuenta?
        (default true)}."""
        body = _read_json_body()
        cod_cta = (body.get('cod_cta') or '').strip()
        if not cod_cta:
            return json_response({'success': False, 'error': 'cod_cta es obligatorio'}, 200)
        cod_plan = (body.get('cod_plan') or 'PGCS').strip()

        Account = request.env['freematica.account'].sudo()
        existing = Account.search([('cod_plan', '=', cod_plan), ('cod_cta', '=', cod_cta)], limit=1)
        if existing:
            return json_response({
                'success': False,
                'error': 'Ya existe la cuenta %s en el plan %s.' % (cod_cta, cod_plan),
            }, 200)

        vals = {
            'cod_plan': cod_plan,
            'cod_cta': cod_cta,
            'des_cta': body.get('des_cta'),
            'des_cta2': body.get('des_cta2'),
            'cta_activa': body.get('cta_activa', True),
            'subcuenta': body.get('subcuenta', True),
        }
        try:
            account = Account.create(vals)
        except Exception as error:
            _logger.error('Freematica create cuenta error: %s', error)
            return json_response({'success': False, 'error': str(error)}, 200)
        return json_response({'success': True, 'data': _account_dict(account)}, 201)

    @http.route('/api/v1/freematica/cuentas/<int:account_id>', type='http', auth='public',
                methods=['PUT', 'OPTIONS'], csrf=False)
    def update_cuenta(self, account_id, **kwargs):
        """PUT /api/v1/freematica/cuentas/<id>
        Edita una cuenta existente. `cod_plan`/`cod_cta` son la clave y nunca
        se editan aquí (crear una nueva si el código estaba mal). Body:
        {des_cta?, des_cta2?, cta_activa?, subcuenta?}."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        body = _read_json_body()
        account = request.env['freematica.account'].sudo().browse(account_id)
        if not account.exists():
            return json_response({'success': False, 'error': 'Cuenta no encontrada'}, 200)
        vals = {k: body[k] for k in ('des_cta', 'des_cta2', 'cta_activa', 'subcuenta') if k in body}
        try:
            account.write(vals)
        except Exception as error:
            _logger.error('Freematica update cuenta error: %s', error)
            return json_response({'success': False, 'error': str(error)}, 200)
        return json_response({'success': True, 'data': _account_dict(account)})

    # ── Reparto de un movimiento bancario sin factura entre cuentas reales ──
    # A diferencia de /api/v1/bank-movements/<id>/categorize (finIA_backend,
    # una etiqueta de texto libre), esto guarda 1+ vínculos reales y
    # validados (freematica.bank.movement.account.line), cada uno con su
    # propio importe — permite repartir un mismo movimiento entre varias
    # cuentas (p.ej. comisión + su IVA), igual que ya se puede repartir un
    # movimiento entre varias facturas. Vive aquí (no en finIA_backend)
    # porque depende del catálogo freematica.account.

    @http.route('/api/v1/freematica/bank-movements/account-lines', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def bulk_bank_movement_account_lines(self, **kwargs):
        """GET .../account-lines?movement_ids=1,2,3
        Devuelve, para cada movimiento pedido, sus líneas de cuenta contable
        asignadas — para que el frontend las cargue en bloque junto con la
        lista de movimientos (mismo patrón que finia.bank.movement.match
        para facturas en el GET /api/v1/bank-movements de finIA_backend)."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        raw_ids = request.params.get('movement_ids', '')
        try:
            movement_ids = [int(x) for x in raw_ids.split(',') if x.strip()]
        except ValueError:
            return json_response({'success': False, 'error': 'movement_ids inválido'}, 200)
        if not movement_ids:
            return json_response({'success': True, 'data': {}})
        lines = request.env['freematica.bank.movement.account.line'].sudo().search([
            ('movement_id', 'in', movement_ids),
        ])
        data = {}
        for line in lines:
            data.setdefault(str(line.movement_id.id), []).append(_account_line_dict(line))
        return json_response({'success': True, 'data': data})

    @http.route('/api/v1/freematica/bank-movements/<int:movement_id>/account-lines', type='http',
                auth='public', methods=['GET', 'POST', 'OPTIONS'], csrf=False)
    def bank_movement_account_lines(self, movement_id, **kwargs):
        """GET: lista las líneas de cuenta contable de este movimiento (para
        abrir el modal ya con lo que tenía asignado). POST body
        {account_id} o {cod_cta, cod_plan?}, más {amount}: agrega UNA línea
        nueva — reparto parcial permitido, se puede llamar varias veces para
        repartir entre distintas cuentas."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        movement = request.env['finia.bank.movement'].sudo().browse(movement_id)
        if not movement.exists():
            return json_response({'success': False, 'error': 'Movimiento bancario no encontrado'}, 200)

        if request.httprequest.method == 'GET':
            return json_response({
                'success': True,
                'data': [_account_line_dict(l) for l in movement.freematica_account_line_ids],
            })

        body = _read_json_body()
        try:
            amount = float(body.get('amount'))
        except (TypeError, ValueError):
            return json_response({'success': False, 'error': 'amount es obligatorio y debe ser numérico'}, 200)
        if amount <= 0:
            return json_response({'success': False, 'error': 'amount debe ser mayor que 0'}, 200)

        Account = request.env['freematica.account'].sudo()
        account = None
        if body.get('account_id'):
            account = Account.browse(int(body['account_id']))
        elif body.get('cod_cta'):
            cod_plan = (body.get('cod_plan') or 'PGCS').strip()
            account = Account.search([
                ('cod_plan', '=', cod_plan), ('cod_cta', '=', (body['cod_cta'] or '').strip()),
            ], limit=1)
        if not account or not account.exists() or not account.cta_activa or not account.subcuenta:
            return json_response({
                'success': False,
                'error': 'La cuenta indicada no existe en el catálogo, no está activa, o no es imputable.',
            }, 200)

        # Igual que el flujo anterior de asignación directa: un movimiento
        # con cuenta(s) contable(s) asignada(s) no puede tener a la vez un
        # vínculo confirmado a factura/albarán/abono/ticket.
        request.env['finia.bank.movement.match'].sudo().search([
            ('movement_id', '=', movement.id), ('state', '=', 'confirmado'),
        ]).write({'state': 'rechazado'})

        line = request.env['freematica.bank.movement.account.line'].sudo().create({
            'movement_id': movement.id, 'freematica_account_id': account.id, 'amount': amount,
        })
        _recompute_movement_from_lines(movement)
        return json_response({
            'success': True,
            'data': {'line': _account_line_dict(line), 'movement': _movement_lines_summary(movement)},
        }, 201)

    @http.route('/api/v1/freematica/bank-movements/<int:movement_id>/account-lines/<int:line_id>', type='http',
                auth='public', methods=['DELETE', 'OPTIONS'], csrf=False)
    def delete_bank_movement_account_line(self, movement_id, line_id, **kwargs):
        """DELETE .../account-lines/<line_id>
        Quita UN reparto puntual (el movimiento puede tener varios) — el
        resto de líneas, si las hay, quedan intactas."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        movement = request.env['finia.bank.movement'].sudo().browse(movement_id)
        if not movement.exists():
            return json_response({'success': False, 'error': 'Movimiento bancario no encontrado'}, 200)
        line = request.env['freematica.bank.movement.account.line'].sudo().browse(line_id)
        if not line.exists() or line.movement_id.id != movement.id:
            return json_response({'success': False, 'error': 'Línea no encontrada'}, 200)
        line.unlink()
        _recompute_movement_from_lines(movement)
        return json_response({'success': True, 'data': _movement_lines_summary(movement)})

    @http.route('/api/v1/invoices/pending-accounting-account', type='http', auth='public', methods=['GET'], csrf=False)
    def pending_accounting_account(self, **kwargs):
        """GET /api/v1/invoices/pending-accounting-account?limit=&offset=
        Facturas contabilizadas, no enviadas todavía a Freematica, con al
        menos una línea sin accounting_account — candidatas para el modal
        masivo de asignación."""
        try:
            limit = int(request.params.get('limit') or 100)
            offset = int(request.params.get('offset') or 0)
        except (TypeError, ValueError):
            return json_response({'success': False, 'error': 'Invalid parameters'}, 200)

        try:
            Invoice = request.env['finia.invoice'].sudo()
            domain = [
                ('state', '=', 'contabilizado'),
                ('freematica_state', '!=', 'enviado'),
                ('line_ids.accounting_account', '=', False),
            ]
            total_count = Invoice.search_count(domain)
            invoices = Invoice.search(domain, limit=limit, offset=offset, order='accounting_date desc, id desc')
            return json_response({
                'success': True,
                'data': {
                    'invoices': [_invoice_pending_dict(invoice) for invoice in invoices],
                    'total_count': total_count,
                    'limit': limit,
                    'offset': offset,
                },
            })
        except Exception as error:
            _logger.error('Freematica pending-accounting-account error: %s', error)
            return json_response({'success': False, 'error': str(error)}, 200)

    @http.route('/api/v1/invoices/<int:invoice_id>/assign-accounting-account', type='http', auth='public',
                methods=['POST'], csrf=False)
    def assign_accounting_account(self, invoice_id, **kwargs):
        """POST /api/v1/invoices/<id>/assign-accounting-account

        Dos modos, según lo que traiga el body:

        - Toda la factura: {"accounting_account": "62900000", "apply_to_vendor_default": true}
          Escribe esa cuenta en TODAS las líneas; si apply_to_vendor_default,
          también en el proveedor OCR (sobreescribe a propósito: es una
          elección explícita confirmada en el frontend).
        - Por línea: {"line_accounts": [{"line_id": 123, "accounting_account": "62900000"}, ...]}
          Escribe cada línea con su propia cuenta; sin default de proveedor
          (no tiene sentido cuando las cuentas difieren por línea).

        En ambos casos valida cada código contra el catálogo real
        (freematica.account) antes de guardar nada. No envía a Freematica —
        eso sigue siendo un paso aparte."""
        body = _read_json_body()
        line_accounts = body.get('line_accounts')

        try:
            Invoice = request.env['finia.invoice'].sudo()
            invoice = Invoice.browse(invoice_id)
            if not invoice.exists():
                return json_response({'success': False, 'error': 'Invoice not found'}, 200)

            Account = request.env['freematica.account'].sudo()

            def _validate_account(code):
                valid = Account.search([
                    ('cod_cta', '=', code), ('cta_activa', '=', True), ('subcuenta', '=', True),
                ], limit=1)
                if not valid:
                    raise ValueError(
                        'La cuenta "%s" no existe en el plan de cuentas de Freematica (o no es una '
                        'cuenta activa/imputable). Sincroniza cuentas si acaba de crearse.' % code
                    )

            if line_accounts:
                line_ids = [int(entry.get('line_id')) for entry in line_accounts if entry.get('line_id')]
                lines_by_id = {line.id: line for line in invoice.line_ids.filtered(lambda l: l.id in line_ids)}
                for entry in line_accounts:
                    line_id = entry.get('line_id')
                    code = (entry.get('accounting_account') or '').strip()
                    if not line_id or not code:
                        continue
                    if int(line_id) not in lines_by_id:
                        return json_response({
                            'success': False,
                            'error': 'La línea %s no pertenece a la factura %s.' % (line_id, invoice_id),
                        }, 200)
                    _validate_account(code)

                for entry in line_accounts:
                    line_id = entry.get('line_id')
                    code = (entry.get('accounting_account') or '').strip()
                    if not line_id or not code:
                        continue
                    lines_by_id[int(line_id)].write({'accounting_account': code})

                return json_response({
                    'success': True,
                    'data': {'invoice_id': invoice.id, 'mode': 'lines', 'line_accounts': line_accounts},
                })

            accounting_account = (body.get('accounting_account') or '').strip()
            apply_to_vendor_default = bool(body.get('apply_to_vendor_default'))
            if not accounting_account:
                return json_response({'success': False, 'error': 'accounting_account is required'}, 200)

            _validate_account(accounting_account)

            invoice.line_ids.write({'accounting_account': accounting_account})
            if apply_to_vendor_default and invoice.ocr_vendor_id:
                invoice.ocr_vendor_id.write({'default_accounting_account': accounting_account})

            return json_response({
                'success': True,
                'data': {
                    'invoice_id': invoice.id,
                    'mode': 'invoice',
                    'accounting_account': accounting_account,
                    'applied_to_vendor_default': apply_to_vendor_default,
                },
            })
        except ValueError as error:
            return json_response({'success': False, 'error': str(error)}, 200)
        except Exception as error:
            _logger.error('Freematica assign-accounting-account error for invoice %s: %s', invoice_id, error)
            return json_response({'success': False, 'error': str(error)}, 200)
