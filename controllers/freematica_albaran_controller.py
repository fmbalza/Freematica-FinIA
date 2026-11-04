# -*- coding: utf-8 -*-
"""Envío de albaranes de compra a Freematica desde el frontend: estado de un
albarán, enviar/reintentar, reiniciar el envío y un resumen de los que
necesitan atención. Responden SIEMPRE HTTP 200 (ver
freematica_accounts_controller.py)."""
import logging

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.finIA_backend.controllers.cors_utils import json_response, cors_preflight_response

_logger = logging.getLogger(__name__)


def _error(message):
    return json_response({'success': False, 'error': message}, 200)


def _albaran(albaran_id):
    albaran = request.env['finia.albaran'].sudo().browse(albaran_id).exists()
    if not albaran:
        raise UserError('El albarán no existe.')
    return albaran


class FreematicaAlbaranController(http.Controller):

    @http.route('/api/v1/albaranes/<int:albaran_id>/freematica', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def albaran_status(self, albaran_id, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            albaran = _albaran(albaran_id)
            config = request.env['freematica.config'].sudo().search([('active', '=', True)], limit=1)
            data = albaran._freematica_api_dict()
            if albaran.freematica_state in ('no_enviado', 'bloqueado') and config:
                # Comprobación al vuelo (sin enviar nada) para mostrar ya qué falta.
                problems, codes, actions, warnings = albaran._freematica_check_albaran(config)
                data.update({
                    'pending_problems': problems,
                    'pending_codes': codes,
                    'pending_actions': actions,
                    'warning': '\n'.join(warnings) or data['warning'],
                })
            data['auto_send'] = bool(config.albaranes_envio_activo)
            data['send_from'] = config.albaranes_fecha_inicio and fields.Date.to_string(config.albaranes_fecha_inicio)
            return json_response({'success': True, 'data': data})
        except UserError as error:
            return _error(str(error))
        except Exception as error:
            _logger.error('Freematica albaran status error (%s): %s', albaran_id, error)
            return _error(str(error))

    @http.route('/api/v1/albaranes/<int:albaran_id>/send-to-freematica', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False)
    def albaran_send(self, albaran_id, **kwargs):
        """Enviar o reintentar (continúa desde la última línea que entró)."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            albaran = _albaran(albaran_id)
            if albaran.source != 'excel':
                return _error('Solo se envían a Freematica los albaranes recibidos en Excel.')
            ok = albaran.action_freematica_send().get(albaran.id)
            albaran.invalidate_recordset()
            data = albaran._freematica_api_dict()
            if ok:
                return json_response({'success': True, 'data': data,
                                      'message': 'Albarán enviado a Freematica (nº %s).' % data['numdoc']})
            return json_response({'success': False, 'data': data,
                                  'error': data['error'] or 'No se pudo enviar el albarán.'}, 200)
        except UserError as error:
            return _error(str(error))
        except Exception as error:
            _logger.exception('Freematica albaran send error (%s)', albaran_id)
            return _error(str(error))

    @http.route('/api/v1/albaranes/<int:albaran_id>/freematica/reset', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False)
    def albaran_reset(self, albaran_id, **kwargs):
        """Tras borrar el albarán en Freematica, para volver a enviarlo entero."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            albaran = _albaran(albaran_id)
            albaran.action_freematica_reset()
            return json_response({'success': True, 'data': albaran._freematica_api_dict()})
        except UserError as error:
            return _error(str(error))
        except Exception as error:
            _logger.error('Freematica albaran reset error (%s): %s', albaran_id, error)
            return _error(str(error))

    @http.route('/api/v1/freematica/albaranes/summary', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def albaranes_summary(self, **kwargs):
        """Cuántos albaranes hay en cada estado (desde la fecha de inicio) y
        los que necesitan atención (falta información o error), agrupados
        por motivo, para el panel de la pantalla de Albaranes."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            config = request.env['freematica.config'].sudo().search([('active', '=', True)], limit=1)
            Albaran = request.env['finia.albaran'].sudo()
            domain = [('source', '=', 'excel')]
            if config.albaranes_fecha_inicio:
                domain.append(('ocr_albaran_date', '>=', config.albaranes_fecha_inicio))
            counts = {state: count for state, count in
                      Albaran._read_group(domain, ['freematica_state'], ['__count'])}
            attention = Albaran.search(domain + [('freematica_state', 'in', ('bloqueado', 'error'))],
                                       order='ocr_albaran_date desc, id desc', limit=200)
            groups = {}
            for albaran in attention:
                key = (albaran.freematica_state, albaran.freematica_error_codes or '',
                       albaran.ocr_vendor_id.name or '')
                group = groups.setdefault(key, {
                    'state': albaran.freematica_state,
                    'codes': [c for c in (albaran.freematica_error_codes or '').split(',') if c],
                    'vendor': albaran.ocr_vendor_id.name or None,
                    'error': albaran.freematica_error,
                    'action': albaran.freematica_action,
                    'albaranes': [],
                })
                group['albaranes'].append({
                    'id': albaran.id,
                    'name': albaran.name,
                    'number': albaran.ocr_albaran_number,
                    'date': albaran.ocr_albaran_date and fields.Date.to_string(albaran.ocr_albaran_date),
                    'error': albaran.freematica_error,
                })
            return json_response({'success': True, 'data': {
                'auto_send': bool(config.albaranes_envio_activo),
                'send_from': config.albaranes_fecha_inicio and fields.Date.to_string(config.albaranes_fecha_inicio),
                'counts': {state: counts.get(state, 0) for state in
                           ('no_enviado', 'bloqueado', 'enviando', 'error', 'enviado')},
                'groups': list(groups.values()),
            }})
        except Exception as error:
            _logger.error('Freematica albaranes summary error: %s', error)
            return _error(str(error))
