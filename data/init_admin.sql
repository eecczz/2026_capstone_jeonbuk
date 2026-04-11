-- Sprinter OWI 관리자 계정 생성
-- sprinter@mail.go.kr / sprint26!

INSERT INTO "user" (id, name, email, role, profile_image_url, created_at, updated_at, last_active_at, settings, info)
VALUES (
    'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
    'Sprinter Admin',
    'sprinter@mail.go.kr',
    'admin',
    '/user.png',
    EXTRACT(EPOCH FROM NOW())::bigint,
    EXTRACT(EPOCH FROM NOW())::bigint,
    EXTRACT(EPOCH FROM NOW())::bigint,
    '{}',
    '{}'
);

INSERT INTO auth (id, email, password, active)
VALUES (
    'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
    'sprinter@mail.go.kr',
    '$2b$12$siee0bPYclCPz3sladEoD.qWW714oq2cy4jxrlMUmBArZMEiP2Yei',
    true
);
