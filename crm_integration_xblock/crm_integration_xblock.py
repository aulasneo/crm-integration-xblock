"""
CRM Integration Xblock.

This module is the actual implementation of the Xblock related classes
"""

import json

from collections import OrderedDict
from urllib.parse import urlencode
from opaque_keys.edx.keys import CourseKey

import requests
import pkg_resources

from xblock.core import XBlock
from xblock.fields import Scope, String
from xblock.fragment import Fragment
from xblockutils.studio_editable import StudioEditableXBlockMixin

from .varkey_validations import SalesForceVarkey
from .salesforce_tasks import SalesForce
from .tracking import emit


BACKENDS = {"generic": SalesForce,
            "varkey": SalesForceVarkey}


class CrmIntegration(StudioEditableXBlockMixin, XBlock):
    """
    This Xblock allow any course unit to send information to an external CRM.
    It acts as a proxy to the external calls, adding the authentication parameters
    and holding the private data we don't want to send to the browsers.
    """

    # Show the XBlock when masquerading as a specific user.
    show_in_read_only_mode = True

    display_name = String(
        display_name="Display Name",
        scope=Scope.settings,
        default="Crm Integration"
    )

    backend_name = String(help="Please write the name of the backend. "
                          "Normally it is the name of the project you are working on",
                          scope=Scope.content,
                          display_name="Name of your project")

    url = String(help="The URL for sandbox is https://test.salesforce.com/services/oauth2/token "
                 "and for production https://login.salesforce.com/services/oauth2/token",
                 scope=Scope.content,
                 display_name="URL")

    client_id = String(help="",
                       scope=Scope.content,
                       display_name="Customer Key")

    client_secret = String(help="Your Customer Secret is found in the app you created in SalesForce "
                           "for Oauth. See instructions here: "
                           "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"
                           "quickstart_oauth.htm",
                           scope=Scope.content,
                           display_name="Customer Secret")

    username = String(help="Your Salesforce's email",
                      scope=Scope.content,
                      display_name="Email")

    password = String(help="Your SalesForce's password",
                      scope=Scope.content,
                      display_name="Password")

    security_token = String(help="Be aware that your security token will change if you change your password",
                            scope=Scope.content,
                            display_name="Security Token")

    editable_fields = ('backend_name',
                       'url',
                       'client_id',
                       'client_secret',
                       'username',
                       'password',
                       'security_token',)

    def resource_string(self, path):
        """Handy helper for getting resources from our kit."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

    def student_view(self, context=None):
        """
        The primary view of the CrmIntegration, shown to students
        when viewing courses.
        """
        # pylint: disable=no-member
        context = self.get_general_rendering_context(context)

        in_studio_runtime = hasattr(self.xmodule_runtime, 'is_author_mode')

        if in_studio_runtime:
            return self.author_view(context)

        html = self.resource_string("static/html/crm-integration-student.html")
        frag = Fragment(html.format(**context))
        frag.add_css(self.resource_string("static/css/crm-integration-xblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/crm-integration-xblock.js"))
        frag.initialize_js('CrmIntegrationLms')
        return frag

    def author_view(self, context=None):
        """
        Returns author view fragment on Studio.
        Should display an example on how to use this xblock.
        """
        # pylint: disable=unused-argument, no-self-use
        html = self.resource_string("static/html/crm-integration-author.html")
        frag = Fragment(html.format(**context))
        frag.add_css(self.resource_string("static/css/crm-integration-xblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/crm-integration-xblock.js"))
        frag.initialize_js('CrmIntegrationStudio')

        return frag

    def generate_token(self, url, client_id, client_secret, username, password, security_token):  # pylint: disable=too-many-arguments
        """
        This method generate an authentication token for SalesForce
        """
        # pylint: disable=unused-argument
        payload = urlencode(OrderedDict(grant_type="password", client_id=client_id,
                                        client_secret=client_secret,
                                        username=username,
                                        password="{}{}".format(password, security_token)))

        headers = {'content-type': "application/x-www-form-urlencoded", }
        response = requests.request("POST", url, data=payload, headers=headers)

        if response.status_code == 200:
            emit("crm_integration_xblock.generate_token.success", 10)
        else:
            emit("crm_integration_xblock.generate_token.error", 30)
        return response

    def _init_fs_class(self, data):
        """
        This method receive data to process CRM request
        """
        is_studio = hasattr(self.xmodule_runtime, 'is_author_mode')  # pylint: disable=no-member
        data_no_init = data.get("no_init", False)
        if is_studio or data_no_init:
            emit("crm_integration_xblock.initialization.no_init", 10)
            return {
                "status_code": 204,
                "message": "No initialization has been run. Token not generated",
                "success": False
            }

        backend_name = self.backend_name
        url = self.url
        client_id = self.client_id
        client_secret = self.client_secret
        username = self.username
        password = self.password
        security_token = self.security_token

        token = self.generate_token(url, client_id, client_secret, username, password, security_token)

        if token.status_code != 200:
            emit("crm_integration_xblock.initialization.{}.no_token_generated".format(backend_name), 10)
            return {"status_code": token.status_code,
                    "message": "Token not generated",
                    "success": False}
        else:
            response_salesforce = json.loads(token.text)
            access_token = response_salesforce["access_token"]
            instance_url = response_salesforce["instance_url"]
            username = self.get_anonymous_id_comp_crm()
            if isinstance(data, str):
                data = json.loads(data)
            method = data.get("method", None)
            initial = data.get("initial", None)
            # pylint: disable=attribute-defined-outside-init
            self.fs_class = BACKENDS[backend_name](
                access_token,
                instance_url,
                username,
                method,
                initial
            )
            emit("crm_integration_xblock.initialization.{}.success".format(backend_name), 10)
            return {"status_code": token.status_code}

    @XBlock.json_handler
    def send_crm_data(self, data, suffix=''):
        """
        This method sends the data to the appropriate backend which in turn sends it to the CRM
        """
        # pylint: disable=unused-argument
        crm_data = self._init_fs_class(data)

        if crm_data["status_code"] == 200:
            return self.fs_class.validate(data)
        else:
            return crm_data

    @XBlock.json_handler
    def delete_crm_data(self, data, suffix=''):
        """
        This method DELETE the data to the appropriate backend which in turn sends it to the CRM
        """
        # pylint: disable=unused-argument
        crm_data = self._init_fs_class(data)
        if crm_data["status_code"] == 200:
            return self.fs_class._delete_data(data)  # pylint: disable=protected-access
        else:
            return crm_data

    def get_general_rendering_context(self, context=None):
        """
        This method creates or adds to the generic context for rendering the html
        """
        if not context:
            context = {}

        context["self"] = self

        # Adapted from lms/urls.py # xblock Handler APIs
        context["lms_handler_url"] = '/courses/{course_key}/xblock/{usage_key}/handler/{handler_name}'.format(
            course_key=self.course_id,  # pylint: disable=no-member
            usage_key=self.url_name,  # pylint: disable=no-member
            handler_name='send_crm_data',  # manually set
        )

        return context

    def get_anonymous_id_comp_crm(self):
        """
        Helper method to obtain the correct compatibility anonymous_id.
        Return the compatibility anonymous_id found in the model of the user, if it exists,
        otherwise returns the current anonymous_id
        """
        current_anonymous_student_id = self.runtime.anonymous_student_id
        user = self.runtime.get_real_user(current_anonymous_student_id)
        course_id_str = str(self.runtime.course_id)
        # Create a compatibility course id with suffix _CRM_XBLOCK
        compat_course_id = CourseKey.from_string('{}_CRM_XBLOCK'.format(course_id_str))
        try:
            # Check if the user has a previous assigned anonymous_id
            compat_anonymous_student_id = user.anonymoususerid_set.get(course_id=compat_course_id)
        except user.anonymoususerid_set.model.DoesNotExist:
            return current_anonymous_student_id
        else:
            return compat_anonymous_student_id.anonymous_user_id

    @staticmethod
    def workbench_scenarios():
        """
        A canned scenario for display in the workbench.
        """
        return [
            ("CrmIntegration",
             """<crm-integration/>
             """),
            ("Multiple CrmIntegration",
             """<vertical_demo>
                <crm-integration/>
                <crm-integration/>
                <crm-integration/>
                </vertical_demo>
             """),
        ]
