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
    empresa_id = fields.Many2one(
        'finia.empresa', string='Empresa (Finia)', index=True, ondelete='restrict',
        help='Empresa de Finia a la que pertenece esta conexión con Freematica (una por empresa '
             'de Freematica: X-Auth-Company). Las facturas y albaranes de la empresa se envían con '
             'esta configuración y sus catálogos (cuentas, proveedores, artículos). Los documentos '
             'sin empresa (anteriores a las empresas) usan la configuración más antigua.',
    )

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
    fecha_inicio_envio = fields.Date(
        string='Enviar facturas recibidas desde',
        help='Solo se envían a Freematica las facturas recibidas (fecha de recepción) desde este '
             'día. Las anteriores ya las registró el cliente directamente en Freematica y '
             'enviarlas las duplicaría. Servinet: desde el 23/10/2026.',
    )
    regla_vencimiento = fields.Selection([
        ('meses', '30 días = 1 mes (31/07 a 30 días vence el 31/08)'),
        ('fin_mes', 'A fin del mes resultante'),
        ('dias', 'Días naturales (31/07 a 30 días vence el 30/08)'),
    ], string='Cálculo del vencimiento', default='meses', required=True,
        help='Cómo se cuentan los días de la forma de pago del proveedor (Formas de pago '
             'Freematica) para la fecha de vencimiento de la Cartera, cuando la factura no la trae.')
    cuenta_irpf = fields.Char(
        string='Cuenta de retenciones IRPF', default='47510002',
        help='Cuenta (Haber) de la retención de IRPF en las facturas con retención. '
             'Servinet: 47510002 "IRPF PROFESSIONALS".')
    # ── Albaranes de compra (POST /comp/v2/albaranes-compras) ───────────
    # Valores fijos confirmados por Josep (Freematica) el 04/11/2026.
    albaranes_envio_activo = fields.Boolean(
        string='Enviar albaranes de compra',
        help='Si está activo, el cron envía a Freematica los albaranes en Excel que cumplan las '
             'condiciones (fecha, proveedor identificado...). Desactivado no se envía nada '
             'automáticamente; el botón de envío manual sigue funcionando.')
    albaranes_fecha_inicio = fields.Date(
        string='Enviar albaranes con fecha desde', default=lambda self: fields.Date.to_date('2026-10-01'),
        help='Fecha del albarán a partir de la cual se envían. Servinet: los servicios de '
             'octubre de 2026 en adelante.')
    albaranes_numdoc_propio = fields.Boolean(
        string='Numerar los albaranes desde Finia (ALCC_NUMDOC)', default=True,
        help='Finia pone el nº de albarán con su propia secuencia (freematica.albaran.numdoc, desde 1). '
             'Desmarcar para mandar 0 y que lo asigne Freematica (solo si su contador de albaranes de compra '
             'tiene numeración).')
    albaranes_delegacion = fields.Char(string='Delegación (ALCC_DELEG)', default='9999')
    albaranes_cod_documento = fields.Char(string='Código documento (ALCC_CODDOC)', default='2')
    albaranes_almacen = fields.Char(string='Almacén (ALCL_CODALMACEN)', default='01')
    albaranes_serie_factura = fields.Char(string='Serie factura (ALCC_SERIEFRA)', default='C0')
    albaranes_loc_pago = fields.Char(string='Localización pago (ALCC_LOC_PAGO)', default='1')
    albaranes_divisa = fields.Char(string='Divisa (ALCC_CODDIVISA)', default='EUR')
    albaranes_lineas_sin_articulo = fields.Selection([
        ('bloquear', 'No enviar el albarán (avisar)'),
        ('texto', 'Enviarlas como línea de texto (C6)'),
    ], string='Líneas sin código de artículo', default='bloquear', required=True,
        help='Josep: C1 = línea con artículo, C6 = descripción o texto sin artículo. Pendiente '
             'de confirmar qué hacer con los albaranes que no traen código (Euro-Mort).')
    articles_last_sync_at = fields.Datetime(string='Última sincronización de artículos', readonly=True)

    contador_documento_listo = fields.Boolean(
        string='Contador de documento listo',
        help='Activar solo después de fijar en la secuencia "freematica.documento.serie" el '
             'próximo nº del ejercicio en curso (el siguiente al último registrado en '
             'Freematica para la serie). Mientras esté desactivado no se envía ninguna factura, '
             'para no repetir números ya usados. Los ejercicios nuevos empiezan en 1 solos.',
    )
    plantilla_normal = fields.Char(
        string='Plantilla — factura normal (BORR_PLANT)', default='30',
        help='Confirmado con Servinet (2026-08-14): "30 - Factura Proveedor ICA" para '
             'facturas normales (sin IRPF). Se usa salvo que el proveedor tenga marcado '
             '"Facturas con IRPF" en su ficha.',
    )
    plantilla_irpf = fields.Char(
        string='Plantilla — factura con IRPF (BORR_PLANT)', default='32',
        help='Confirmado con Servinet (2026-08-14): "32 - Factura Proveedor ICA+IRPF" para '
             'facturas de proveedores marcados como sujetos a retención de IRPF.',
    )
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
    # Reanudable por páginas: cada corrida (botón o cron) procesa lo que
    # entre en un presupuesto de tiempo seguro y guarda en qué página se
    # quedó (providers_sync_next_page). Al llegar al final de una pasada
    # completa, el cursor vuelve a 1 y no se arranca una pasada nueva hasta
    # que pase providers_sync_interval_hours desde la última completada —
    # evita tanto superar limit_time_real como resincronizar sin necesidad.
    providers_last_sync_at = fields.Datetime(string='Última sincronización completa', readonly=True)
    providers_last_sync_count = fields.Integer(string='Proveedores sincronizados', readonly=True)
    providers_last_sync_error = fields.Text(string='Último error de sincronización', readonly=True)
    providers_sync_next_page = fields.Integer(string='Próxima página a sincronizar', default=1, readonly=True)
    providers_sync_count_so_far = fields.Integer(string='Sincronizados en la pasada actual', default=0, readonly=True)
    providers_sync_interval_hours = fields.Integer(
        string='Repetir pasada completa cada (horas)', default=24,
        help='Una vez terminada una pasada completa, no se arranca otra hasta que pasen '
             'estas horas. El botón manual "Sincronizar proveedores" siempre fuerza el '
             'inicio de una pasada (o continúa la que esté en curso) sin importar esto.',
    )

    # ── Sincronización del plan de cuentas (freematica.account) ─────────
    accounts_last_sync_at = fields.Datetime(string='Última sincronización completa', readonly=True)
    accounts_last_sync_count = fields.Integer(string='Cuentas sincronizadas', readonly=True)
    accounts_last_sync_error = fields.Text(string='Último error de sincronización (cuentas)', readonly=True)
    accounts_sync_next_page = fields.Integer(string='Próxima página a sincronizar', default=1, readonly=True)
    accounts_sync_count_so_far = fields.Integer(string='Sincronizadas en la pasada actual', default=0, readonly=True)
    accounts_sync_interval_hours = fields.Integer(
        string='Repetir pasada completa cada (horas)', default=24,
        help='Una vez terminada una pasada completa, no se arranca otra hasta que pasen '
             'estas horas. El botón manual "Sincronizar cuentas" siempre fuerza el inicio '
             'de una pasada (o continúa la que esté en curso) sin importar esto.',
    )

    @api.model
    def get_config_for(self, empresa=None):
        """Configuración de Freematica de una empresa de Finia (recordset o id).

        - Con empresa: la configuración activa ligada a esa empresa; si no hay
          ninguna, error (nunca se cae a la de otra empresa: enviaría los datos
          a la empresa equivocada de Freematica).
        - Sin empresa (documentos anteriores a las empresas): la configuración
          activa más antigua.
        """
        empresa_id = getattr(empresa, 'id', empresa) or False
        configs = self.search([('active', '=', True)], order='id')
        if empresa_id:
            match = configs.filtered(lambda c: c.empresa_id.id == empresa_id)[:1]
            if match:
                return match
            empresa_name = self.env['finia.empresa'].sudo().browse(empresa_id).display_name
            raise UserError(
                'La empresa "%s" no tiene configuración de Freematica. Crea una en '
                'FinIA - Freematica > Configuración y asígnale esa empresa.' % empresa_name)
        if not configs:
            raise UserError('No hay ninguna configuración de Freematica activa. Crea una en '
                            'FinIA - Freematica > Configuración.')
        return configs[:1]

    @api.model
    def get_active_config(self):
        """Configuración por defecto (la más antigua): para los sitios que aún no
        saben de qué empresa son. Preferir `get_config_for(empresa)`."""
        return self.get_config_for(None)

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

    def _sync_notification(self, title, next_page, count_so_far, last_count):
        if next_page <= 1:
            message = 'Pasada completa terminada: %d sincronizados.' % last_count
            notif_type = 'success'
        else:
            message = (
                'En progreso: %d sincronizados hasta ahora, continúa en la página %d. '
                'Se sigue solo al volver a apretar el botón, o lo retoma el cron automáticamente.'
            ) % (count_so_far, next_page)
            notif_type = 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': notif_type, 'sticky': next_page > 1},
        }

    def action_sync_providers(self):
        self.ensure_one()
        try:
            self.env['freematica.provider'].sync_from_freematica(self, force=True)
        except client.FreematicaError as error:
            raise UserError(error.mensaje) from error
        return self._sync_notification(
            'Freematica: proveedores', self.providers_sync_next_page,
            self.providers_sync_count_so_far, self.providers_last_sync_count,
        )

    @api.model
    def action_refresh_providers_async(self):
        """Fuerza una pasada completa del catálogo de proveedores en el
        próximo ciclo del cron y lo lanza ya (ver refresh del panel de avisos)."""
        self.ensure_one()
        self.sudo().write({'providers_sync_next_page': 1, 'providers_last_sync_at': False})
        cron = self.env.ref('Freematica-FinIA.ir_cron_freematica_sync_providers', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return True

    def action_sync_articles(self):
        self.ensure_one()
        try:
            count = self.env['freematica.article'].sync_from_freematica(self)
        except client.FreematicaError as error:
            raise UserError(error.mensaje) from error
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': 'Freematica', 'message': '%d artículos sincronizados.' % count, 'type': 'success'}}

    def _cron_sync_articles(self):
        for config in self.search([('active', '=', True)]):
            try:
                self.env['freematica.article'].sync_from_freematica(config)
            except Exception as error:  # noqa: BLE001 - un fallo de sync no debe tumbar el cron
                _logger.error('Freematica: fallo sincronizando artículos (config %s): %s', config.id, error)

    def _cron_sync_providers(self):
        configs = self.search([('active', '=', True)])
        for config in configs:
            try:
                self.env['freematica.provider'].sync_from_freematica(config, force=False)
            except Exception as error:  # noqa: BLE001 - un fallo de sync no debe tumbar el cron
                _logger.error('Freematica: fallo sincronizando proveedores (config %s): %s', config.id, error)
        # Con el catálogo al día, reintentar los proveedores sin coincidencia.
        self.env['finia.ocr.vendor']._freematica_match_pending()

    def action_sync_accounts(self):
        self.ensure_one()
        try:
            self.env['freematica.account'].sync_from_freematica(self, force=True)
        except client.FreematicaError as error:
            raise UserError(error.mensaje) from error
        return self._sync_notification(
            'Freematica: plan de cuentas', self.accounts_sync_next_page,
            self.accounts_sync_count_so_far, self.accounts_last_sync_count,
        )

    @api.model
    def _cron_sync_accounts(self):
        configs = self.search([('active', '=', True)])
        for config in configs:
            try:
                self.env['freematica.account'].sync_from_freematica(config, force=False)
            except Exception as error:  # noqa: BLE001 - un fallo de sync no debe tumbar el cron
                _logger.error('Freematica: fallo sincronizando cuentas (config %s): %s', config.id, error)
