# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError

from ..services import freematica_client as client


class FreematicaSendableMixin(models.AbstractModel):
    """Estado y orquestación de envío a Freematica, común a los modelos que
    lo implementen. Por ahora solo lo usa `finia.invoice`
    (`freematica_invoice.py`). Requiere que el modelo concreto implemente
    `_freematica_validate_before_send()` y
    `_freematica_build_asiento_payload(config)`."""
    _name = 'freematica.sendable.mixin'
    _description = 'Finia Freematica Sendable Mixin'

    freematica_state = fields.Selection([
        ('no_enviado', 'No enviado'),
        ('enviando', 'Enviando'),
        ('enviado', 'Enviado'),
        ('error', 'Error'),
    ], string='Estado Freematica', default='no_enviado', copy=False)
    freematica_sent_at = fields.Datetime(string='Enviado a Freematica el', readonly=True)
    freematica_error = fields.Text(string='Último error Freematica', readonly=True)
    freematica_borr_cod = fields.Char(string='Código de asiento (BORR_COD)', readonly=True, copy=False)

    def _freematica_validate_before_send(self):
        """A implementar por cada modelo concreto."""
        raise NotImplementedError

    def _freematica_build_asiento_payload(self, config):
        """A implementar por cada modelo concreto. Debe devolver el dict
        VoAsientoBorrador completo (cabecera BORR_* + LINEAS)."""
        raise NotImplementedError

    def _freematica_log(self, log_type, message):
        self.ensure_one()
        vals = {
            'origin_user_id': self.partner_id.id,
            'log_type': log_type,
            'message': message,
        }
        if self._name == 'finia.invoice':
            vals['invoice_id'] = self.id
        self.env['finia.log'].sudo().create(vals)

    def _freematica_send_one(self):
        """Envía un único registro a Freematica. No usa `self.ensure_one()`
        como guardia de lote (se llama registro por registro desde el
        caller) — devuelve siempre `{'success', 'message'?, 'error'?}`,
        nunca lanza, para que un fallo no tumbe el resto de un envío
        múltiple."""
        self.ensure_one()
        try:
            self._freematica_validate_before_send()
        except UserError as error:
            message = str(error)
            self.write({'freematica_state': 'error', 'freematica_error': message})
            self._freematica_log('freematica_error', message)
            return {'success': False, 'error': message}

        try:
            config = self.env['freematica.config'].get_active_config()
        except UserError as error:
            message = str(error)
            self.write({'freematica_state': 'error', 'freematica_error': message})
            self._freematica_log('freematica_error', message)
            return {'success': False, 'error': message}

        self.write({'freematica_state': 'enviando'})
        try:
            token = config._ensure_valid_token()
            payload = self._freematica_build_asiento_payload(config)
            client_config = config._as_client_config()
            response = client.import_asientos(client_config, payload, token=token)
        except (client.FreematicaError, UserError) as error:
            message = getattr(error, 'mensaje', None) or str(error)
            self.write({'freematica_state': 'error', 'freematica_error': message})
            self._freematica_log('freematica_error', message)
            return {'success': False, 'error': message}
        except Exception as error:  # noqa: BLE001 - un fallo no debe bloquear el resto del lote
            message = str(error)
            self.write({'freematica_state': 'error', 'freematica_error': message})
            self._freematica_log('freematica_error', message)
            return {'success': False, 'error': message}

        borr_cod = (response.get('BORR_COD') if isinstance(response, dict) else None) or payload.get('BORR_COD')
        self.write({
            'freematica_state': 'enviado',
            'freematica_sent_at': fields.Datetime.now(),
            'freematica_error': False,
            'freematica_borr_cod': borr_cod,
        })
        self._freematica_log('freematica_sent', 'Enviado a Freematica (BORR_COD %s)' % borr_cod)
        return {'success': True, 'message': 'Enviado a Freematica correctamente (BORR_COD %s).' % borr_cod}
