# -*- coding: utf-8 -*-
import json
import logging
import werkzeug.utils
import werkzeug.wrappers
from datetime import datetime
from odoo import http, fields
from odoo.addons.web.controllers.main import ensure_db
from ..oauth2.validator import OdooValidator

_logger = logging.getLogger(__name__)

try:
    import oauthlib
    from oauthlib import oauth2
except ImportError:
    _logger.debug('Cannot `import oauthlib`.')


class OAuth2ProviderController(http.Controller):
    def __init__(self):
        super(OAuth2ProviderController, self).__init__()

    def _get_request_information(self):
        """ Retrieve needed arguments for oauthlib methods """
        uri = http.request.httprequest.base_url
        http_method = http.request.httprequest.method
        body = oauthlib.common.urlencode(
            http.request.httprequest.values.items())
        headers = http.request.httprequest.headers

        return uri, http_method, body, headers

    def _check_access_token(self, access_token):
        """ Check if the provided access token is valid """
        token = http.request.env['oauth.provider.token'].search([
            ('token', '=', access_token),
        ])
        if not token:
            return False

        oauth2_server = token.client_id.get_oauth2_server()
        # Retrieve needed arguments for oauthlib methods
        uri, http_method, body, headers = self._get_request_information()

        # Validate request information
        valid, oauthlib_request = oauth2_server.verify_request(
            uri, http_method=http_method, body=body, headers=headers)

        if valid:
            return token

        return False

    def _json_response(self, data=None, status=200, headers=None):
        """ Returns a json response to the client """
        if headers is None:
            headers = {'Content-Type': 'application/json'}

        return werkzeug.wrappers.BaseResponse(
            json.dumps(data), status=status, headers=headers)

    @http.route('/oauth2/authorize', type='http', auth='user', methods=['GET'])
    def authorize(self, client_id=None, response_type=None, redirect_uri=None,
                  scope=None, state=None, *args, **kwargs):
        """ Check client's request, and display an authorization page to the user,

        The authorization page lists allowed scopes
        If the client is configured to skip the authorization page, directly
        redirects to the requested URI
        """
        client = http.request.env['oauth.provider.client'].search([
            ('identifier', '=', client_id),
        ])
        if not client:
            return http.request.render(
                'oauth_provider.authorization_error', {
                    'title': 'Unknown Client Identifier!',
                    'message': 'This client identifier is invalid.',
                })
        oauth2_server = client.get_oauth2_server()

        # Retrieve needed arguments for oauthlib methods
        uri, http_method, body, headers = self._get_request_information()
        try:
            scopes, credentials = oauth2_server.validate_authorization_request(
                uri, http_method=http_method, body=body, headers=headers)
            # Store only some values, because the pickling of the full request
            # object is not possible
            http.request.session['oauth_scopes'] = scopes
            http.request.session['oauth_credentials'] = {
                'client_id': credentials['client_id'],
                'redirect_uri': credentials['redirect_uri'],
                'response_type': credentials['response_type'],
                'state': credentials['state'],
            }
            if client.skip_authorization:
                # Skip the authorization page
                # Useful when the application is trusted
                return self.authorize_post()
        except oauth2.FatalClientError as e:
            return http.request.render(
                'oauth_provider.authorization_error', {
                    'title': 'Error: {error}'.format(error=e.error),
                    'message': e.description,
                })
        except oauth2.OAuth2Error as e:
            return http.request.render(
                'oauth_provider.authorization_error', {
                    'title': 'Error: {error}'.format(error=e.error),
                    'message': 'An unknown error occured! Please contact your '
                    'administrator',
                })

        oauth_scopes = client.scope_ids.filtered(
            lambda record: record.code in scopes)
        return http.request.render(
            'oauth_provider.authorization', {
                'oauth_client': client.name,
                'oauth_scopes': oauth_scopes,
            })

    @http.route(
        '/oauth2/authorize', type='http', auth='user', methods=['POST'])
    def authorize_post(self, *args, **kwargs):
        """ Redirect to the requested URI during the authorization """
        client = http.request.env['oauth.provider.client'].search([
            ('identifier', '=', http.request.session.get(
                'oauth_credentials', {}).get('client_id'))])
        if not client:
            return http.request.render(
                'oauth_provider.authorization_error', {
                    'title': 'Unknown Client Identifier!',
                    'message': 'This client identifier is invalid.',
                })
        oauth2_server = client.get_oauth2_server()

        # Retrieve needed arguments for oauthlib methods
        uri, http_method, body, headers = self._get_request_information()
        scopes = http.request.session['oauth_scopes']
        credentials = http.request.session['oauth_credentials']
        headers, body, status = oauth2_server.create_authorization_response(
            uri, http_method=http_method, body=body, headers=headers,
            scopes=scopes, credentials=credentials)

        return werkzeug.utils.redirect(headers['Location'], code=status)

    @http.route(
        '/oauth2/deny', type='json', auth='user', methods=['POST'])
    def deny_post(self, submit_button="oauth_deny", *args, **kwargs):
        """ Redirect to the requested URI during the authorization """
        if submit_button == "oauth_deny":
            data = dict([('grant', 400)])
            data["redirect_uri"] = http.request.session.get('oauth_credentials').get('redirect_uri')
            return json.dumps(data)

    @http.route('/oauth2/token', type='http', auth='none', methods=['POST'],
                csrf=False)
    def token(self, client_id=None, client_secret=None, redirect_uri=None,
              scope=None, code=None, grant_type=None, username=None,
              password=None, refresh_token=None, *args, **kwargs):
        """ Return a token corresponding to the supplied information

        Not all parameters are required, depending on the application type
        """
        ensure_db()
        # If no client_id is specified, get it from session
        if client_id is None:
            client_id = http.request.session.get(
                'oauth_credentials', {}).get('client_id')
        client = http.request.env['oauth.provider.client'].sudo().search([
            ('identifier', '=', client_id),
        ])
        if not client:
            return self._json_response(
                data={'error': 'invalid_client_id'}, status=401)
        oauth2_server = client.get_oauth2_server(token_expires_in=client.token_expires_in)
        # Retrieve needed arguments for oauthlib methods
        uri, http_method, body, headers = self._get_request_information()
        credentials = {'scope': scope}
        # Retrieve the authorization code, if any, to get Odoo's user id
        existing_code = http.request.env[
            'oauth.provider.authorization.code'].search([
                ('client_id.identifier', '=', client_id),
                ('code', '=', code),
            ])
        if existing_code:
            credentials['user_id'] = existing_code.user_id.id
        # Retrieve the existing token, if any, to get Odoo's user id
        existing_token = http.request.env['oauth.provider.token'].search([
            ('client_id.identifier', '=', client_id),
            ('refresh_token', '=', refresh_token),
        ])
        if existing_token:
            credentials['user_id'] = existing_token.user_id.id
        headers, body, status = oauth2_server.create_token_response(
            uri, http_method=http_method, body=body, headers=headers,
            credentials=credentials)
        _logger.warning("headers " + str(headers) + " body: " + str(body) + " status: " + str(status))
        return werkzeug.wrappers.BaseResponse(
            body, status=status, headers=headers)

    @http.route('/oauth2/tokeninfo', type='http', auth='none', methods=['GET'])
    def tokeninfo(self, access_token=None, *args, **kwargs):
        """ Return some information about the supplied token

        Similar to Google's "tokeninfo" request
        """
        ensure_db()
        token = self._check_access_token(access_token)
        if not token:
            return self._json_response(
                data={'error': 'invalid_or_expired_token'}, status=401)

        token_lifetime = (fields.Datetime.from_string(token.expires_at) -
                          datetime.now()).total_seconds()
        # Base data to return
        data = {
            'audience': token.client_id.identifier,
            'scopes': ' '.join(token.scope_ids.mapped('code')),
            'expires_in': token_lifetime,
        }

        # Add the oauth user identifier, if user's information access is
        # allowed by the token's scopes
        user_data = token.get_data_for_model(
            'res.users', res_id=token.user_id.id)
        if 'id' in user_data:
            data.update(user_id=token.generate_user_id())
            data.update(name=user_data["name"])
            data.update(email=user_data["login"])
        return self._json_response(data=data)

    @http.route('/oauth2/userinfo', type='http', auth='none', methods=['GET'])
    def userinfo(self, access_token=None, *args, **kwargs):
        """ Return some information about the user linked to the supplied token

        Similar to Google's "userinfo" request
        """
        ensure_db()
        auth = http.request.httprequest.headers.get('Authorization', ' ')
        auth_type, auth_string = auth.split(' ', 1)
        if auth_type != 'Basic':
            _logger.warning("Bearer")
            access_token = auth_string
            _logger.warning(access_token)
        token = self._check_access_token(access_token)
        if not token:
            return self._json_response(
                data={'error': 'invalid_or_expired_token'}, status=401)

        data = token.get_data_for_model('res.users', res_id=token.user_id.id)
        return self._json_response(data=data)

    @http.route('/oauth2/otherinfo', type='http', auth='none', methods=['GET'])
    def otherinfo(self, access_token=None, model=None, *args, **kwargs):
        """ Return allowed information about the requested model """
        ensure_db()
        token = self._check_access_token(access_token)
        if not token:
            return self._json_response(
                data={'error': 'invalid_or_expired_token'}, status=401)

        model_obj = http.request.env['ir.model'].search([
            ('model', '=', model),
        ])
        if not model_obj:
            return self._json_response(
                data={'error': 'invalid_model'}, status=400)

        data = token.get_data_for_model(model)
        return self._json_response(data=data)

    @http.route(
        '/oauth2/revoke_token', type='http', auth='none', methods=['POST'], csrf=False)
    def revoke_token(self, token=None, *args, **kwargs):
        """ Revoke the supplied token """
        ensure_db()
        body = oauthlib.common.urlencode(
            http.request.httprequest.values.items())
        db_token = http.request.env['oauth.provider.token'].search([
            ('token', '=', token),
        ])
        if not db_token:
            db_token = http.request.env['oauth.provider.token'].search([
                ('refresh_token', '=', token),
            ])
        if not db_token:
            return self._json_response(
                data={'error': 'invalid_or_expired_token'}, status=401)
        oauth2_server = db_token.client_id.get_oauth2_server()

        # Retrieve needed arguments for oauthlib methods
        uri, http_method, body, headers = self._get_request_information()

        headers, body, status = oauth2_server.create_revocation_response(
            uri, http_method=http_method, body=body, headers=headers)
        return werkzeug.wrappers.BaseResponse(
            body, status=status, headers=headers)
