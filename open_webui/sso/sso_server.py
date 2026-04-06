import os, time, hmac, hashlib, urllib.parse, uuid, datetime, json, base64
from flask import Flask, request, redirect, make_response
from open_webui.models.users import Users
from open_webui.models.auths import Auths as AuthsModel
from open_webui.utils.auth import get_password_hash

app = Flask(__name__)

SSO_SHARED_SECRET = os.environ.get("SSO_SHARED_SECRET", "wjsqnrai2025")
ALLOWED_SKEW_SEC = int(os.environ.get("SSO_ALLOWED_SKEW_SEC", "600"))

WEBUI_SECRET_KEY = os.environ.get("WEBUI_SECRET_KEY") or os.environ.get("WEBUI_JWT_SECRET_KEY") or ""
if not WEBUI_SECRET_KEY:
    raise RuntimeError("WEBUI_SECRET_KEY (or WEBUI_JWT_SECRET_KEY) is required")

def verify_hmac(uid: str, ts: str, token: str) -> bool:
    msg = f"{uid}|{ts}".encode("utf-8")
    mac = hmac.new(SSO_SHARED_SECRET.encode("utf-8"), msg, hashlib.sha1).hexdigest()
    return hmac.compare_digest(mac, token)

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def parse_duration_to_seconds(s: str) -> int:
    if not s or s.strip() == "" or s.strip() == "-1":
        return -1
    s = s.strip().lower()
    unit = s[-1]
    try:
        num = int(s[:-1]) if unit.isalpha() else int(s)
    except:
        return -1
    if not unit.isalpha():
        return num
    if unit == "s": return num
    if unit == "m": return num * 60
    if unit == "h": return num * 3600
    if unit == "d": return num * 86400
    if unit == "w": return num * 7 * 86400
    return -1

def create_jwt_for_user_id(user_id: str) -> str:
    now = int(time.time())
    jwt_expires_in = os.environ.get("JWT_EXPIRES_IN", "-1")
    ttl = parse_duration_to_seconds(jwt_expires_in)
    
    payload = {"id": user_id, "sub": user_id, "iat": now}
    if ttl and ttl > 0:
        payload["exp"] = now + ttl
    
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    
    sig = hmac.new(WEBUI_SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = b64url(sig)
    return f"{header_b64}.{payload_b64}.{sig_b64}"

# ============================================================
# 핵심 변경: HTML + JS로 localStorage에 토큰 저장 후 리다이렉트
# ============================================================

SSO_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>SSO 로그인 중...</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }}
        .container {{
            text-align: center;
            background: white;
            padding: 40px 60px;
            border-radius: 16px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }}
        .spinner {{
            width: 50px;
            height: 50px;
            border: 4px solid #f3f3f3;
            border-top: 4px solid #667eea;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }}
        @keyframes spin {{
            0% {{ transform: rotate(0deg); }}
            100% {{ transform: rotate(360deg); }}
        }}
        h2 {{ color: #333; margin-bottom: 10px; }}
        p {{ color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="spinner"></div>
        <h2>로그인 처리 중</h2>
        <p>잠시만 기다려 주세요...</p>
    </div>
    <script>
        (function() {{
            try {{
                // OpenWebUI가 사용하는 localStorage 키에 토큰 저장
                localStorage.setItem('token', '{jwt_token}');
                
                // 디버깅용 (운영에서는 제거 가능)
                console.log('[SSO] Token saved to localStorage');
                
                // 메인 페이지로 이동
                window.location.href = '/';
            }} catch (e) {{
                console.error('[SSO] Failed to save token:', e);
                document.body.innerHTML = '<div class="container"><h2>오류 발생</h2><p>로그인 처리 중 문제가 발생했습니다.</p><p><a href="/">다시 시도</a></p></div>';
            }}
        }})();
    </script>
</body>
</html>"""

SSO_ERROR_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>SSO 오류</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: #f5f5f5;
        }}
        .container {{
            text-align: center;
            background: white;
            padding: 40px 60px;
            border-radius: 16px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        }}
        h2 {{ color: #e74c3c; }}
        p {{ color: #666; }}
        a {{ color: #667eea; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>⚠️ 로그인 실패</h2>
        <p>{error_message}</p>
        <p><a href="/">메인으로 돌아가기</a></p>
    </div>
</body>
</html>"""


@app.get("/sso-login")
def sso_login():
    uid = request.args.get("uid", "")
    ts = request.args.get("ts", "")
    token = request.args.get("token", "")

    # 파라미터 검증
    if not uid or not ts or not token:
        resp = make_response(SSO_ERROR_HTML.format(error_message="필수 파라미터가 누락되었습니다."), 400)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    try:
        ts_ms = int(ts)
    except:
        resp = make_response(SSO_ERROR_HTML.format(error_message="잘못된 타임스탬프입니다."), 400)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    # 시간 검증
    now_ms = int(time.time() * 1000)
    if abs(now_ms - ts_ms) > ALLOWED_SKEW_SEC * 1000:
        resp = make_response(SSO_ERROR_HTML.format(error_message="SSO 링크가 만료되었습니다. 다시 시도해 주세요."), 401)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    uid = urllib.parse.unquote(uid).strip().lower()

    # HMAC 검증
    if not verify_hmac(uid, ts, token):
        resp = make_response(SSO_ERROR_HTML.format(error_message="인증 토큰이 유효하지 않습니다."), 401)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    # 사용자 조회/생성
    user = Users.get_user_by_email(uid)
    if not user:
        pw = str(uuid.uuid4())
        hashed = get_password_hash(pw)
        AuthsModel.insert_new_auth(uid, hashed, uid, None, "user")
        user = Users.get_user_by_email(uid)

    if not user:
        resp = make_response(SSO_ERROR_HTML.format(error_message="사용자 생성에 실패했습니다."), 500)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    # JWT 생성
    jwt_token = create_jwt_for_user_id(str(user.id))

    # ★ 핵심: HTML 응답으로 localStorage에 토큰 저장
    html_content = SSO_SUCCESS_HTML.format(jwt_token=jwt_token)
    resp = make_response(html_content, 200)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    
    # 캐시 방지
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    
    return resp


# 헬스체크 엔드포인트 (선택)
@app.get("/sso-health")
def health():
    return {"status": "ok", "timestamp": int(time.time())}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4000)
