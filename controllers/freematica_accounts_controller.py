# -*- coding: utf-8 -*-
"""Endpoints propios (no overrides) para el flujo de asignación manual de
cuenta contable: buscar en el plan de cuentas real de Freematica, listar
facturas pendientes de asignación, y guardar lo que el usuario elija —
sin disparar el envío real a Freematica (eso sigue siendo un paso aparte,
explícito, desde `send-to-freematica`)."""
import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.finIA_backend.controllers.cors_utils import json_response, cors_preflight_response

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
        cuentas activas e imputables — para el selector del frontend."""
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
