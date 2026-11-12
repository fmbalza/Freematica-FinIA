# -*- coding: utf-8 -*-
from odoo import fields, models


class FreematicaN43Export(models.Model):
    """Registro de cada Norma 43 enviado (o intentado) a Freematica: sirve de
    historial y para no reenviar dos veces el mismo fichero."""
    _name = 'freematica.n43.export'
    _description = 'Envío de Norma 43 a Freematica'
    _order = 'id desc'

    connection_id = fields.Many2one(
        'finia.bank.connection', string='Conexión bancaria', required=True, index=True, ondelete='cascade')
    date_from = fields.Date(string='Desde', required=True)
    date_to = fields.Date(string='Hasta', required=True)
    cod_banco = fields.Char(string='COD_BANCO', required=True)
    filename = fields.Char(string='Fichero', required=True)
    file_hash = fields.Char(string='SHA-256 del fichero', required=True, index=True)
    movements_count = fields.Integer(string='Movimientos')
    state = fields.Selection([('enviado', 'Enviado'), ('error', 'Error')], default='enviado', required=True)
    response = fields.Text(string='Respuesta de Freematica')
    sent_at = fields.Datetime(string='Enviado el', default=fields.Datetime.now)
