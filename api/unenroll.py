import json
import os
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests
from google.protobuf.message import DecodeError

import device_management_backend_pb2 as proto

CLIENT_ID = "77185425430.apps.googleusercontent.com"
CLIENT_SECRET = "OTJgUOQcT7lO7GsGZq2G4IlT"
TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"
DM_API = "https://m.google.com/devicemanagement/data/api"
SCOPES = "https://www.googleapis.com/auth/chromeosdevicemanagement https://www.googleapis.com/auth/userinfo.email"

PROXY_API_KEY = "nex_live_b1448735b70db775"
PROXY_API_URL = "https://console.nextproxy.site/api/random"


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def get_proxy():
    headers = {
        "X-API-Key": PROXY_API_KEY,
        "Accept": "application/json"
    }
    try:
        response = requests.get(PROXY_API_URL, headers=headers, timeout=10)
        if response.status_code == 200:
            node = response.json()
            ip = node.get('ip')
            port = node.get('port')
            p_type = node.get('type', 'http').lower()
            
            proxy_str = f"{p_type}://{ip}:{port}"
            return {"http": proxy_str, "https": proxy_str}
    except Exception:
        pass
    return None


def token_exchange(payload, proxies=None):
    r = requests.post(TOKEN_URL, data=payload, timeout=15, proxies=proxies)
    try:
        body = r.json()
    except ValueError:
        raise ApiError(f"token endpoint returned non-JSON, status {r.status_code}", 502)
    if r.status_code != 200:
        raise ApiError(f"token exchange failed: {body.get('error', r.status_code)}")
    return body


def run_unenroll(serial_number, oauth_code):
    # fetch proxy once per unenroll attempt
    proxies = get_proxy()

    refresh = token_exchange({
        "code": oauth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
    }, proxies=proxies)
    refresh_token = refresh.get("refresh_token")
    if not refresh_token:
        raise ApiError("no refresh_token returned - oauth code is invalid or already used")

    access = token_exchange({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPES,
    }, proxies=proxies)
    oauth_token = access.get("access_token")
    if not oauth_token:
        raise ApiError("no access_token from refresh exchange", 502)

    device_id = str(uuid.uuid4())

    register_request = proto.DeviceRegisterRequest()
    register_request.type = proto.DeviceRegisterRequest.DEVICE
    register_request.machine_id = serial_number

    request = proto.DeviceManagementRequest()
    request.register_request.CopyFrom(register_request)

    try:
        r = requests.post(
            f"{DM_API}?devicetype=2&apptype=Chrome&request=register&deviceid={device_id}&oauth_token={oauth_token}",
            headers={"Content-Type": "application/protobuf"},
            data=request.SerializeToString(),
            timeout=20,
            proxies=proxies,
        )
        data = proto.DeviceManagementResponse()
        data.ParseFromString(r.content)
    except requests.RequestException as e:
        raise ApiError(f"register request failed: {e}", 502)
    except DecodeError as e:
        raise ApiError(f"register response unreadable: {e}", 502)

    if data.error_message:
        raise ApiError(data.error_message)

    dmtoken = data.register_response.device_management_token
    if not dmtoken:
        raise ApiError("no device_management_token in register response", 502)

    policy_fetch_request = proto.PolicyFetchRequest()
    policy_fetch_request.policy_type = "google/chromeos/device"

    state_key_update_request = proto.DeviceStateKeyUpdateRequest()
    for _ in range(5):
        state_key_update_request.server_backed_state_keys.append(os.urandom(32))

    request = proto.DeviceManagementRequest()
    request.policy_request.requests.append(policy_fetch_request)
    request.device_state_key_update_request.CopyFrom(state_key_update_request)

    try:
        requests.post(
            f"{DM_API}?retry=false&apptype=Chrome&deviceid={device_id}&devicetype=2&request=policy",
            headers={
                "Authorization": f"GoogleDMToken token={dmtoken}",
                "Content-Type": "application/protobuf",
            },
            data=request.SerializeToString(),
            timeout=20,
            proxies=proxies,
        )
    except requests.RequestException:
        pass  # response ignored, same as the original script

    return {"device_id": device_id, "dmtoken": dmtoken}


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        self._dispatch(
            (qs.get("sn") or [""])[0].strip(),
            (qs.get("oauth") or [""])[0].strip(),
        )

    def do_POST(self):
        qs = parse_qs(urlparse(self.path).query)
        serial_number = (qs.get("sn") or [""])[0].strip()
        oauth_code = (qs.get("oauth") or [""])[0].strip()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length))
                serial_number = (body.get("sn") or serial_number or "").strip()
                oauth_code = (body.get("oauth") or oauth_code or "").strip()
            except ValueError:
                pass
        self._dispatch(serial_number, oauth_code)

    def _dispatch(self, serial_number, oauth_code):
        if not serial_number or not oauth_code:
            self._json(400, {"success": False,
                             "error": "missing params - use ?sn=SERIAL&oauth=OAUTH_CODE"})
            return
        try:
            result = run_unenroll(serial_number, oauth_code)
            self._json(200, {"success": True, **result})
        except ApiError as e:
            self._json(e.status, {"success": False, "error": str(e)})
        except Exception as e:
            self._json(500, {"success": False, "error": f"internal: {e}"})

    def log_message(self, *args):
        pass
