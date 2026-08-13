# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services import freematica_client as client

_logger = logging.getLogger(__name__)


class FreematicaConfig(models.Model):
    _name = 'freematica.config'
    _description = 'Configuración Freematica'
    _order = 'id'

    name = fields.Char(default='Freematica', required=True)
    active = fields.Boolean(default=True)

    # ── Conexión ───────────────────────────────────────────────────────
    host = fields.Char(
        string='Host de la API', required=True,
        default='https://api-p02.clientservicepanel.com/restsat/api',
        help='Confirmado por soporte de Freematica (2026-08-13) como el host real de '
             'Servinet — verificado con una llamada real a export-asientos que devolvió '
             '200 y datos reales. Ni api-config.freefy.cloud ni api.freematica.io eran '
             'correctos: eran los dos hosts que sugería la documentación/Swagger, pero no '
             'coincidían con el entorno real asignado a Servinet.',
    )
    x_auth_organization = fields.Char(string='X-Auth-Organization', required=True)
    x_auth_company = fields.Char(
        string='X-Auth-Company', required=True,
        help='También se usa como BORR_CODEMP (empresa) en cada asiento enviado.',
    )
    x_auth_session = fields.Char(
        string='X-Auth-Session',
        help='No documentada en la especificación OpenAPI, pero presente en las '
             'credenciales de prueba de Servinet. Se envía si está configurada.',
    )
    x_auth_app = fields.Char(
        string='X-Auth-App',
        help='No documentada en la especificación OpenAPI, pero presente en las '
             'credenciales de prueba de Servinet. Se envía si está configurada.',
    )
    language = fields.Char(string='Language', default='es')
    request_timeout = fields.Integer(string='Timeout (s)', default=20)

    # ── Autenticación: token manual o login automático ──────────────────
    access_token = fields.Char(
        string='Access Token', groups='base.group_system', copy=False,
        help='Se puede pegar directamente un token ya obtenido (p.ej. el de pruebas). '
             'Si además se configuran usuario/contraseña, "Renovar token" pedirá uno '
             'nuevo por login en vez de reusar este.',
    )
    token_obtained_at = fields.Datetime(string='Token obtenido el', readonly=True, copy=False)
    login_username = fields.Char(string='Usuario (login)')
    login_password = fields.Char(string='Password (login)', groups='base.group_system')

    # ── Datos contables por defecto (obligatorios, sin valor de fábrica:
    # dependen del plan de cuentas real de Servinet en Freematica) ──────
    diario_compras = fields.Char(
        string='Diario contable (BORR_DIARIO)', required=True,
        help='Código del diario de compras/proveedores a usar en los asientos. '
             'Confirmar el código real con Freematica/GSATEK — no hay valor por defecto '
             'seguro, para no contabilizar en un diario equivocado.',
    )
    cuenta_proveedor_default = fields.Char(
        string='Cuenta contable de Proveedores (BORRL_CTA) — respaldo', required=True,
        help='Cuenta a usar SOLO si el proveedor de la factura no se pudo matchear contra '
             'el catálogo de Freematica (freematica.provider.cta_contable). Confirmado '
             '2026-08-13: la cuenta real varía por proveedor (algunos "40000000", otros '
             '"41000000"), no es una cuenta única para todos — por eso esto es solo un '
             'respaldo, no la fuente principal.',
    )
    cuenta_iva_soportado_default = fields.Char(
        string='Cuenta de IVA Soportado (BORRL_CTA)', required=True,
        help='Cuenta genérica de IVA soportado (p.ej. "472000"), usada en la línea de '
             'IVA de cada asiento.',
    )
    serie_contable_default = fields.Char(
        string='Serie contable (BORRL_SER)', required=True,
        help='Serie a usar en BORRL_SER. Confirmado con una prueba real (2026-08-13, '
             'asiento 7050): Freematica valida que la serie EXISTA en su propio catálogo '
             '("[VALIDACION] Serie no existe") — no se puede derivar de finia.serie (son '
             'catálogos de sistemas distintos), hay que usar una serie real de Servinet en '
             'Freematica.',
    )
    grupo_aux_proveedor = fields.Integer(
        string='Grupo auxiliar de Proveedores (BORRL_GRUPAUX)',
        help='Opcional. Se omite del payload si se deja en 0.',
    )
    concepto_asiento_default = fields.Char(string='Concepto de asiento (BORRL_CONASI)', default='FACT')
    provider_match_threshold = fields.Float(
        string='Umbral de coincidencia de proveedores', default=0.75,
        help='Ratio de similitud (0-1) mínimo para aceptar automáticamente una coincidencia '
             'de proveedor por nombre (mismo mecanismo que facturaweb.config.match_threshold). '
             'Solo aplica al fuzzy-match por nombre; una coincidencia por NIF exacto siempre '
             'se acepta.',
    )

    # ── Formato de LINEAS/IVA/CARTERA (punto ambiguo del propio doc) ────
    lineas_as_json_string = fields.Boolean(
        string='Enviar LINEAS como string JSON en vez de array nativo',
        help='El esquema publicado tipa LINEAS/IVA/CARTERA/ANALITICA como string pero la '
             'descripción dice que son listas de objetos — hay que probar contra el '
             'entorno real cuál de las dos variantes acepta. Empezar sin marcar (array '
             'nativo, lo más estándar) y activar solo si el entorno de pruebas lo rechaza.',
    )

    # ── Resultado de la última prueba de conexión ───────────────────────
    last_test_at = fields.Datetime(string='Última prueba de conexión', readonly=True)
    last_test_ok = fields.Boolean(string='Última prueba OK', readonly=True)
    last_test_message = fields.Text(string='Resultado última prueba', readonly=True)

    # ── Sincronización de proveedores (freematica.provider) ─────────────
    providers_last_sync_at = fields.Datetime(string='Última sincronización de proveedores', readonly=True)
    providers_last_sync_count = fields.Integer(string='Proveedores sincronizados', readonly=True)
    providers_last_sync_error = fields.Text(string='Último error de sincronización', readonly=True)

    # ── Sincronización del plan de cuentas (freematica.account) ─────────
    accounts_last_sync_at = fields.Datetime(string='Última sincronización de cuentas', readonly=True)
    accounts_last_sync_count = fields.Integer(string='Cuentas sincronizadas', readonly=True)
    accounts_last_sync_error = fields.Text(string='Último error de sincronización (cuentas)', readonly=True)

    @api.model
    def get_active_config(self):
        config = self.search([('active', '=', True)], limit=1)
        if not config:
            raise UserError('No hay ninguna configuración de Freematica activa. Crea una en '
                             'FinIA - Freematica > Configuración.')
        return config

    def _as_client_config(self):
        self.ensure_one()
        return {
            'host': self.host,
            'x_auth_organization': self.x_auth_organization,
            'x_auth_company': self.x_auth_company,
            'x_auth_session': self.x_auth_session,
            'x_auth_app': self.x_auth_app,
            'language': self.language,
            'access_token': self.sudo().access_token,
            'login_username': self.login_username,
            'login_password': self.sudo().login_password,
            'request_timeout': self.request_timeout or 20,
            'lineas_as_json_string': self.lineas_as_json_string,
        }

    def _ensure_valid_token(self):
        """Devuelve un access_token usable: el pegado manualmente si existe,
        o uno nuevo por login si hay usuario/contraseña configurados."""
        self.ensure_one()
        client_config = self._as_client_config()
        if client_config.get('access_token'):
            return client_config['access_token']
        if client_config.get('login_username') and client_config.get('login_password'):
            result = client.login(client_config)
            self.sudo().write({
                'access_token': result['access_token'],
                'token_obtained_at': fields.Datetime.now(),
            })
            return result['access_token']
        raise UserError(
            'Freematica: no hay access_token configurado ni usuario/contraseña de login. '
            'Completa alguno de los dos en FinIA - Freematica > Configuración.'
        )

    def action_renew_token(self):
        for record in self:
            client_config = record._as_client_config()
            result = client.login(client_config)
            record.sudo().write({
                'access_token': result['access_token'],
                'token_obtained_at': fields.Datetime.now(),
            })
        return True

    def action_test_connection(self):
        for record in self:
            try:
                token = record._ensure_valid_token()
                client_config = record._as_client_config()
                result = client.test_connection(client_config, token=token)
                record.write({
                    'last_test_at': fields.Datetime.now(),
                    'last_test_ok': True,
                    'last_test_message': str(result)[:2000],
                })
            except client.FreematicaError as error:
                record.write({
                    'last_test_at': fields.Datetime.now(),
                    'last_test_ok': False,
                    'last_test_message': error.mensaje,
                })
                raise UserError(error.mensaje) from error
        return True

    def action_sync_providers(self):
        for record in self:
            try:
                self.env['freematica.provider'].sync_from_freematica(record)
            except client.FreematicaError as error:
                raise UserError(error.mensaje) from error
        return True

    @api.model
    def _cron_sync_providers(self):
        configs = self.search([('active', '=', True)])
        for config in configs:
            try:
                self.env['freematica.provider'].sync_from_freematica(config)
            except Exception as error:  # noqa: BLE001 - un fallo de sync no debe tumbar el cron
                _logger.error('Freematica: fallo sincronizando proveedores (config %s): %s', config.id, error)

    def action_sync_accounts(self):
        for record in self:
            try:
                self.env['freematica.account'].sync_from_freematica(record)
            except client.FreematicaError as error:
                raise UserError(error.mensaje) from error
        return True

    @api.model
    def _cron_sync_accounts(self):
        configs = self.search([('active', '=', True)])
        for config in configs:
            try:
                self.env['freematica.account'].sync_from_freematica(config)
            except Exception as error:  # noqa: BLE001 - un fallo de sync no debe tumbar el cron
                _logger.error('Freematica: fallo sincronizando cuentas (config %s): %s', config.id, error)
