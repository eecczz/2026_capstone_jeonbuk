#!/bin/bash
set -e

# =============================
# 1. PostgreSQL 시작
# =============================
PG_DATA="/var/lib/postgresql/18/main"
PG_CONF="/etc/postgresql/18/main"

if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    echo "[sprinter] PostgreSQL 초기화..."
    su - postgres -c "/usr/lib/postgresql/18/bin/initdb -D $PG_DATA --encoding=UTF-8 --locale=C.UTF-8"
fi

# pg_hba.conf: 로컬 trust
echo "local all all trust
host all all 127.0.0.1/32 md5
host all all ::1/128 md5" > "$PG_DATA/pg_hba.conf"

# PostgreSQL 시작
su - postgres -c "/usr/lib/postgresql/18/bin/pg_ctl -D $PG_DATA -l /var/log/postgresql/pg.log start" || true
sleep 2

# DB/유저 생성 (최초 실행 시)
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='admin'\" | grep -q 1" || \
    su - postgres -c "psql -c \"CREATE ROLE admin WITH LOGIN SUPERUSER PASSWORD 'sprint26!'\""
su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='customui'\" | grep -q 1" || \
    su - postgres -c "psql -c \"CREATE DATABASE customui OWNER admin\""

# =============================
# 2. Redis 시작
# =============================
echo "[sprinter] Redis 시작..."
redis-server --daemonize yes --save "" --appendonly no

# =============================
# 3. SSH 시작
# =============================
echo "[sprinter] SSH 시작..."
ssh-keygen -A 2>/dev/null
/usr/sbin/sshd

# =============================
# 4. 사용자 환경 설정
# =============================
if [ ! -f /home/sprint/.bashrc_sprinter ]; then
    cat >> /home/sprint/.bashrc << 'BASH_EOF'
export ANTHROPIC_API_KEY=dummy-key-for-router
export DATABASE_URL=postgresql://admin:sprint26!@localhost:5432/customui
export PYTHONPATH=/app/backend
alias owi-restart='sudo pkill -f uvicorn; cd /app/backend && sudo -E python3 -m uvicorn open_webui.main:app --host 0.0.0.0 --port 8080 --workers 4 &'
alias owi-log='tail -f /var/log/owi.log'
alias owi-status='curl -sf http://localhost:8080/health && echo " OK" || echo " DOWN"'
echo "=== Sprinter 개발 환경 ==="
echo "  OWI 소스: /app/backend/"
echo "  프론트엔드: /app/build/"
echo "  OWI 재시작: owi-restart"
echo "  OWI 상태: owi-status"
echo "  OWI 로그: owi-log"
echo "  DB 접속: psql -U admin -d customui"
BASH_EOF
    touch /home/sprint/.bashrc_sprinter
    chown sprint:sprint /home/sprint/.bashrc /home/sprint/.bashrc_sprinter
fi

# =============================
# 5. OWI 시작 (foreground)
# =============================
echo "[sprinter] OWI 시작..."
cd /app/backend

# 필수 패키지 확인
pip3 install --break-system-packages -q uvicorn[standard] flask 2>/dev/null || true

export ENABLE_PERSISTENT_CONFIG="True"
export ENABLE_LOGIN_FORM="True"
export ENABLE_PASSWORD_AUTH="True"
export ENABLE_WEBSOCKET_SUPPORT="true"
export WEBSOCKET_MANAGER="redis"
export WEBSOCKET_REDIS_URL="redis://localhost:6379/1"

exec python3 -m uvicorn open_webui.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --forwarded-allow-ips '*' \
    --timeout-keep-alive 65 \
    --workers ${UVICORN_WORKERS:-4} \
    2>&1 | tee /var/log/owi.log
