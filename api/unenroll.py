import json
import os
import uuid
from http.server import BaseHTTPRequestHandler

import requests


GOOGLE_TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"
DEVICE_API_URL = "https://m.google.com/devicemanagement/data/api"
REQUEST_TIMEOUT = (5, 15)


def json_response(handler, status_code, payload):
    body = json.dumps(payload).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def get_request_body(handler):
    content_length = int(handler.headers.get("Content-Length", "0"))

    if content_length <= 0:
        raise ValueError("Request body is empty.")

    raw_body = handler.rfile.read(content_length)

    try:
        return json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Request body must contain valid JSON.") from error


def get_required_environment_variable(name):
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(f"Missing Vercel environment variable: {name}")

    return value


def exchange_oauth_code_for_refresh_token(oauth_code, client_id, client_secret):
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": oauth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
        },
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"OAuth code exchange failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    response_data = response.json()
    refresh_token = response_data.get("refresh_token")

    if not refresh_token:
        raise RuntimeError("Google did not return a refresh token.")

    return refresh_token


def exchange_refresh_token_for_access_token(
    refresh_token,
    client_id,
    client_secret,
):
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": (
                "https://www.googleapis.com/auth/chromeosdevicemanagement "
                "https://www.googleapis.com/auth/userinfo.email"
            ),
        },
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Refresh-token exchange failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    response_data = response.json()
    access_token = response_data.get("access_token")

    if not access_token:
        raise RuntimeError("Google did not return an access token.")

    return access_token


def register_device(proto, serial_number, oauth_token):
    device_id = str(uuid.uuid4())

    register_request = proto.DeviceRegisterRequest()
    register_request.type = proto.DeviceRegisterRequest.DEVICE
    register_request.machine_id = serial_number

    management_request = proto.DeviceManagementRequest()
    management_request.register_request.CopyFrom(register_request)

    response = requests.post(
        (
            f"{DEVICE_API_URL}?devicetype=2&apptype=Chrome"
            f"&request=register&deviceid={device_id}"
            f"&oauth_token={oauth_token}"
        ),
        headers={
            "Content-Type": "application/protobuf",
        },
        data=management_request.SerializeToString(),
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Device registration failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    management_response = proto.DeviceManagementResponse()

    try:
        management_response.ParseFromString(response.content)
    except Exception as error:
        raise RuntimeError(
            "Google returned an invalid protobuf response during registration."
        ) from error

    if management_response.error_message:
        raise RuntimeError(management_response.error_message)

    device_management_token = (
        management_response.register_response.device_management_token
    )

    if not device_management_token:
        raise RuntimeError(
            "Google did not return a device management token."
        )

    return device_id, device_management_token


def update_device_state_keys(proto, device_id, device_management_token):
    policy_fetch_request = proto.PolicyFetchRequest()
    policy_fetch_request.policy_type = "google/chromeos/device"

    state_key_update_request = proto.DeviceStateKeyUpdateRequest()

    for _ in range(5):
        state_key_update_request.server_backed_state_keys.append(
            os.urandom(32)
        )

    management_request = proto.DeviceManagementRequest()
    management_request.policy_request.requests.append(policy_fetch_request)
    management_request.device_state_key_update_request.CopyFrom(
        state_key_update_request
    )

    response = requests.post(
        (
            f"{DEVICE_API_URL}?retry=false&apptype=Chrome"
            f"&deviceid={device_id}&devicetype=2&request=policy"
        ),
        headers={
            "Authorization": (
                f"GoogleDMToken token={device_management_token}"
            ),
            "Content-Type": "application/protobuf",
        },
        data=management_request.SerializeToString(),
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Device policy update failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 200, {"success": True})

    def do_POST(self):
        try:
            # Import inside the request handler so import errors are returned
            # as JSON instead of causing an opaque startup failure.
            try:
                from . import device_management_backend_pb2 as proto
            except ImportError:
                import device_management_backend_pb2 as proto

            params = get_request_body(self)

            if not isinstance(params, dict):
                raise ValueError("Request JSON must be an object.")

            serial_number = str(params.get("serial_number", "")).strip()
            oauth_code = str(params.get("oauth_code", "")).strip()

            if not serial_number:
                raise ValueError("Missing serial_number.")

            if not oauth_code:
                raise ValueError("Missing oauth_code.")

            client_id = get_required_environment_variable(
                "GOOGLE_CLIENT_ID"
            )
            client_secret = get_required_environment_variable(
                "GOOGLE_CLIENT_SECRET"
            )

            refresh_token = exchange_oauth_code_for_refresh_token(
                oauth_code,
                client_id,
                client_secret,
            )

            oauth_token = exchange_refresh_token_for_access_token(
                refresh_token,
                client_id,
                client_secret,
            )

            device_id, device_management_token = register_device(
                proto,
                serial_number,
                oauth_token,
            )

            update_device_state_keys(
                proto,
                device_id,
                device_management_token,
            )

            json_response(
                self,
                200,
                {
                    "success": True,
                    "message": "Device unenrollment request completed.",
                },
            )

        except requests.Timeout:
            json_response(
                self,
                504,
                {
                    "success": False,
                    "error": "A request to Google timed out.",
                },
            )

        except requests.RequestException as error:
            json_response(
                self,
                502,
                {
                    "success": False,
                    "error": f"Google request failed: {str(error)}",
                },
            )

        except Exception as error:
            json_response(
                self,
                500,
                {
                    "success": False,
                    "error": f"{type(error).__name__}: {str(error)}",
                },
            )

    def do_GET(self):
        json_response(
            self,
            405,
            {
                "success": False,
                "error": "Use POST for this endpoint.",
            },
        )

    def log_message(self, format_string, *args):
        # Prevent credentials or request details from being written to logs.
        return
