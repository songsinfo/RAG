import http.server
import json
import urllib.request
import urllib.error
import sys

TARGET_HOST = "https://llm-9cxyg8783n1crke5.cn-beijing.maas.aliyuncs.com"
PORT = 5050

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        auth_header = self.headers.get("Authorization", "")
        
        if self.path == "/rerank":
            # Map /rerank to DashScope rerank endpoint
            target_url = f"{TARGET_HOST}/api/v1/services/rerank/text-rerank/text-rerank"
            req = urllib.request.Request(
                target_url,
                data=body,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_body = resp.read()
                    data = json.loads(resp_body.decode("utf-8"))
                    # Unwrap output.results to top-level results for ModelGateway
                    results = data.get("output", {}).get("results", data.get("results", []))
                    usage = data.get("usage", {})
                    adapted = {
                        "results": results,
                        "usage": usage
                    }
                    adapted_bytes = json.dumps(adapted).encode("utf-8")
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(adapted_bytes)))
                    self.end_headers()
                    self.wfile.write(adapted_bytes)
            except urllib.error.HTTPError as e:
                err_body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
            except Exception as e:
                err_msg = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_msg)))
                self.end_headers()
                self.wfile.write(err_msg)
        else:
            # Forward other endpoints (/chat/completions, /embeddings, etc.) to compatible-mode/v1
            clean_path = self.path
            if not clean_path.startswith("/compatible-mode/v1"):
                clean_path = "/compatible-mode/v1" + clean_path
            target_url = f"{TARGET_HOST}{clean_path}"
            
            if clean_path.endswith("/chat/completions") and body:
                try:
                    req_data = json.loads(body.decode("utf-8"))
                    if "enable_thinking" not in req_data:
                        req_data["enable_thinking"] = False
                    req_data["model"] = "qwen3.5-omni-flash"
                    body = json.dumps(req_data).encode("utf-8")
                except Exception:
                    pass

            headers = {
                "Authorization": auth_header,
                "Content-Type": self.headers.get("Content-Type", "application/json")
            }
            req = urllib.request.Request(target_url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    resp_body = resp.read()
                    self.send_response(resp.status)
                    for h, v in resp.headers.items():
                        if h.lower() not in ["content-length", "transfer-encoding", "connection"]:
                            self.send_header(h, v)
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
            except urllib.error.HTTPError as e:
                err_body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
            except Exception as e:
                err_msg = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_msg)))
                self.end_headers()
                self.wfile.write(err_msg)

    def do_GET(self):
        auth_header = self.headers.get("Authorization", "")
        clean_path = self.path
        if not clean_path.startswith("/compatible-mode/v1"):
            clean_path = "/compatible-mode/v1" + clean_path
        target_url = f"{TARGET_HOST}{clean_path}"
        req = urllib.request.Request(target_url, headers={"Authorization": auth_header})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for h, v in resp.headers.items():
                    if h.lower() not in ["content-length", "transfer-encoding", "connection"]:
                        self.send_header(h, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            err_msg = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_msg)))
            self.end_headers()
            self.wfile.write(err_msg)

    def log_message(self, format, *args):
        pass # suppress default logging to keep stdout clean

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), ProxyHandler)
    print(f"Model adapter proxy running on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
