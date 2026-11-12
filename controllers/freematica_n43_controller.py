# -*- coding: utf-8 -*-
import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.finIA_backend.controllers.config_utils import require_config_read, require_config_write
from odoo.addons.finIA_backend.controllers.cors_utils import cors_preflight_response, json_response

from ..services import freematica_client as client

_logger = logging.getLogger(__name__)


def _body():
    try:
        return json.loads(request.httprequest.data.decode('utf-8')) if request.httprequest.data else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _is_true(value):
    """Freematica manda los booleanos como texto ('0'/'1'): bool('0') sería True."""
    return str(value).strip().lower() in ('1', 'true', 's', 'si', 'sí', 'y', 'yes')


def _connection(connection_id, tenant_id):
    return request.env['finia.bank.connection'].sudo().search([
        ('id', '=', connection_id), ('company_partner_id', '=', tenant_id),
    ], limit=1)


class FreematicaN43Controller(http.Controller):
    """Norma 43 hacia Freematica. Responde siempre HTTP 200 con `success`, como el
    resto de endpoints de este módulo (el cliente HTTP del front descarta el
    cuerpo de los no-2xx)."""

    @http.route('/api/v1/freematica/bancos', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False)
    def list_bancos(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        _partner, _tenant, error = require_config_read()
        if error:
            return error
        try:
            config = request.env['freematica.config'].sudo().get_active_config()
            response = client.list_bancos(config._as_client_config(), token=config._ensure_valid_token())
        except Exception as e:  # noqa: BLE001
            return json_response({'success': False, 'error': getattr(e, 'mensaje', None) or str(e)}, 200)
        items = ((response or {}).get('data') or {}).get('items') or []
        return json_response({'success': True, 'data': [{
            'cod_banco': item.get('COD_BANCO'),
            'nombre': item.get('NOMBRE'),
            'entidad': item.get('CCC_BANCO'),
            'oficina': item.get('CCC_OFICINA'),
            'cuenta_bancaria': item.get('CCC_CUENTA'),
            'cuenta_contable': item.get('CUENTA'),
            'iban': item.get('CCC_IBAN'),
            'inactiva': _is_true(item.get('CMB_INACTIVA')),
        } for item in items]}, 200)

    @http.route('/api/v1/bank-connections/<int:connection_id>/freematica-banco', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False)
    def set_cod_banco(self, connection_id, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        _partner, tenant_id, error = require_config_write()
        if error:
            return error
        connection = _connection(connection_id, tenant_id)
        if not connection:
            return json_response({'success': False, 'error': 'Conexión bancaria no encontrada'}, 200)
        cod_banco = str(_body().get('cod_banco') or '').strip()
        connection.write({'freematica_cod_banco': cod_banco or False})
        return json_response({'success': True, 'data': {'id': connection.id, 'cod_banco': connection.freematica_cod_banco}}, 200)

    @http.route('/api/v1/bank-connections/<int:connection_id>/send-n43-freematica', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False)
    def send_n43(self, connection_id, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        _partner, tenant_id, error = require_config_write()
        if error:
            return error
        connection = _connection(connection_id, tenant_id)
        if not connection:
            return json_response({'success': False, 'error': 'Conexión bancaria no encontrada'}, 200)
        body = _body()
        try:
            result = connection.action_freematica_send_n43(
                body.get('date_from'), body.get('date_to'), force=bool(body.get('force')))
        except Exception as e:  # noqa: BLE001
            return json_response({'success': False, 'error': getattr(e, 'name', None) or str(e)}, 200)
        return json_response({'success': True, 'data': {
            'sent': result['sent'], 'already_sent': result['already_sent'],
            'movements': result['count'], 'export_id': result['export_id'],
        }}, 200)

    @http.route('/api/v1/bank-connections/<int:connection_id>/n43-exports', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def list_n43_exports(self, connection_id, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        _partner, tenant_id, error = require_config_read()
        if error:
            return error
        connection = _connection(connection_id, tenant_id)
        if not connection:
            return json_response({'success': False, 'error': 'Conexión bancaria no encontrada'}, 200)
        return json_response({'success': True, 'data': {
            'cod_banco': connection.freematica_cod_banco or None,
            'exports': [{
                'id': e.id, 'date_from': str(e.date_from), 'date_to': str(e.date_to), 'filename': e.filename,
                'movements': e.movements_count, 'state': e.state, 'sent_at': str(e.sent_at),
                'error': e.response if e.state == 'error' else None,
            } for e in connection.freematica_n43_export_ids[:50]],
        }}, 200)
