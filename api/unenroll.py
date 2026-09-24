import json
import uuid
import os
import requests
from http.server import BaseHTTPRequestHandler
import device_management_backend_pb2 as proto

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            params = json.loads(post_data)
            serial_number = params.get('serial_number')
            oauth_code = params.get('oauth_code')
            
            if not serial_number or not oauth_code:
                raise ValueError("missing serial_number or oauth_code")

            # 1. exchange oauth code for refresh token
            token_res = requests.post(
                "https://www.googleapis.com/oauth2/v4/token",
                data={
                    "code": oauth_code,
                    "client_id": "77185425430.apps.googleusercontent.com",
                    "client_secret": "OTJgUOQcT7lO7GsGZq2G4IlT",
                    "grant_type": "authorization_code",
                },
            )
            token_res.raise_for_status()
            refresh_token = token_res.json()["refresh_token"]

            # 2. exchange refresh token for access token
            auth_res = requests.post(
                "https://www.googleapis.com/oauth2/v4/token",
                data={
                    "client_id": "77185425430.apps.googleusercontent.com",
                    "client_secret": "OTJgUOQcT7lO7GsGZq2G4IlT",
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "https://www.googleapis.com/auth/chromeosdevicemanagement https://www.googleapis.com/auth/userinfo.email",
                },
            )
            auth_res.raise_for_status()
            oauth_token = auth_res.json()["access_token"]

            # 3. register device
            device_id = str(uuid.uuid4())
            register_request = proto.DeviceRegisterRequest()
            register_request.type = proto.DeviceRegisterRequest.DEVICE
            register_request.machine_id = serial_number
            
            request = proto.DeviceManagementRequest()
            request.register_request.CopyFrom(register_request)
            
            reg_res = requests.post(
                f"https://m.google.com/devicemanagement/data/api?devicetype=2&apptype=Chrome&request=register&deviceid={device_id}&oauth_token={oauth_token}",
                headers={"Content-Type": "application/protobuf"},
                data=request.SerializeToString(),
            )
            
            data = proto.DeviceManagementResponse()
            data.ParseFromString(reg_res.content)
            
            if data.error_message:
                raise Exception(data.error_message)

            dmtoken = data.register_response.device_management_token
            
            # 4. wipe state keys (unenroll)
            policy_fetch_request = proto.PolicyFetchRequest()
            policy_fetch_request.policy_type = "google/chromeos/device"
            
            state_key_update_request = proto.DeviceStateKeyUpdateRequest()
            for _ in range(5):
                state_key_update_request.server_backed_state_keys.append(os.urandom(32))
                
            request = proto.DeviceManagementRequest()
            request.policy_request.requests.append(policy_fetch_request)
            request.device_state_key_update_request.CopyFrom(state_key_update_request)
            
            requests.post(
                f"https://m.google.com/devicemanagement/data/api?retry=false&apptype=Chrome&deviceid={device_id}&devicetype=2&request=policy",
                headers={
                    "Authorization": f"GoogleDMToken token={dmtoken}",
                    "Content-Type": "application/protobuf",
                },
                data=request.SerializeToString(),
            )
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": "device unenrolled successfully"}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
