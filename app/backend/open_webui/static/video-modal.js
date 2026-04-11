/*video_generation_modal_start*/
(function() {
    'use strict';

    var VIDEO_KEYWORDS = ['동영상', 'LTX', 'Helios'];
    var MODAL_ID = 'video-gen-modal';
    var _prevModel = '';
    var _modalDismissed = false;
    var _lastUrl = location.href;

    var PRESETS = {
        'LTX': {
            name: 'LTX-2.3',
            resolutions: [
                { label: '360p', w: 640, h: 360 },
                { label: '480p', w: 854, h: 480 },
                { label: '720p', w: 1280, h: 720 },
                { label: '1080p', w: 1920, h: 1080 }
            ],
            defaultRes: '720p',
            basePx: 768*512*241, baseTime: 60
        },
        'Helios': {
            name: 'Helios',
            resolutions: [
                { label: '384p', w: 640, h: 384 },
                { label: '512p', w: 768, h: 512 },
                { label: 'HD', w: 1280, h: 768 }
            ],
            defaultRes: '384p',
            basePx: 640*384*264, baseTime: 30
        }
    };

    function getSelectedModelName() {
        var btn = document.querySelector('button[aria-label="모델 선택"]');
        if (btn) {
            var txt = (btn.textContent || '').trim();
            if (txt.length > 3 && txt.length < 200) return txt;
        }
        var navBtns = document.querySelectorAll('nav button');
        for (var i = 0; i < navBtns.length; i++) {
            var t = (navBtns[i].textContent || '').trim();
            if (t.includes('(') && t.includes(')') && t.length > 5 && t.length < 200) return t;
        }
        // URL 파라미터에서 모델명 가져오기
        var params = new URLSearchParams(location.search);
        return decodeURIComponent(params.get('models') || '');
    }

    function chatHasMessages() {
        return document.querySelectorAll('[data-message-id]').length > 0;
    }

    function getModelType(name) {
        if (name.includes('LTX')) return 'LTX';
        if (name.includes('Helios') || name.includes('helios')) return 'Helios';
        return null;
    }

    function estimateTime(type, duration, resLabel) {
        var cfg = PRESETS[type];
        if (!cfg) return '?';
        var res = cfg.resolutions.find(function(r) { return r.label === resLabel; });
        if (!res) return '?';
        var px = res.w * res.h * duration * 30;
        var sec = Math.round(px / cfg.basePx * cfg.baseTime);
        if (sec >= 60) return Math.floor(sec/60) + '분 ' + (sec%60) + '초';
        return sec + '초';
    }

    function showModal(type) {
        if (document.getElementById(MODAL_ID)) return;
        var cfg = PRESETS[type];
        if (!cfg) return;

        var isDark = document.documentElement.classList.contains('dark');
        var bg = isDark ? '#1e1e2e' : '#ffffff';
        var fg = isDark ? '#e0e0e0' : '#1a1a1a';
        var sub = isDark ? '#999' : '#666';
        var border = isDark ? '#333' : '#e5e7eb';
        var inputBg = isDark ? '#2a2a3e' : '#f9fafb';
        var activeBg = isDark ? '#4f46e5' : '#4f46e5';

        var overlay = document.createElement('div');
        overlay.id = MODAL_ID;
        Object.assign(overlay.style, {
            position:'fixed',top:'0',left:'0',width:'100vw',height:'100vh',
            background:'rgba(0,0,0,0.5)',zIndex:'99999',
            display:'flex',alignItems:'center',justifyContent:'center',
            backdropFilter:'blur(3px)',opacity:'0',transition:'opacity 0.2s'
        });

        var durations = [3, 5, 10, 15, 20];
        var selectedDur = 5;
        var selectedRes = cfg.defaultRes;

        var card = document.createElement('div');
        Object.assign(card.style, {
            background:bg,borderRadius:'16px',padding:'28px',
            maxWidth:'480px',width:'92%',
            boxShadow:'0 24px 80px rgba(0,0,0,0.4)',
            transform:'translateY(10px) scale(0.97)',
            transition:'transform 0.2s ease-out',color:fg
        });

        function renderCard() {
            // Save current prompt before re-render
            var savedPrompt = '';
            var existingPrompt = document.getElementById('vmod-prompt');
            if (existingPrompt) savedPrompt = existingPrompt.value;

            var est = estimateTime(type, selectedDur, selectedRes);
            card.innerHTML =
                '<h3 style="margin:0 0 6px;font-size:18px;font-weight:700;">🎬 ' + cfg.name + ' 영상 생성</h3>' +
                '<p style="margin:0 0 16px;font-size:13px;color:'+sub+';">설정을 선택하고 프롬프트를 입력하세요.</p>' +

                '<label style="display:block;font-size:12px;font-weight:600;color:'+sub+';margin-bottom:6px;">📝 프롬프트</label>' +
                '<textarea id="vmod-prompt" placeholder="만들고 싶은 영상을 설명하세요..." rows="3" ' +
                'style="width:100%;padding:10px 12px;border:1px solid '+border+';border-radius:10px;' +
                'background:'+inputBg+';font-size:14px;resize:none;outline:none;color:'+fg+';' +
                'box-sizing:border-box;margin-bottom:14px;"></textarea>' +

                '<label style="display:block;font-size:12px;font-weight:600;color:'+sub+';margin-bottom:8px;">⏱️ 영상 길이</label>' +
                '<div id="vmod-dur" style="display:flex;gap:6px;margin-bottom:14px;">' +
                durations.map(function(d) {
                    var active = d === selectedDur;
                    return '<button data-dur="'+d+'" style="flex:1;padding:8px 0;border-radius:8px;border:none;' +
                        'font-size:13px;font-weight:600;cursor:pointer;transition:all 0.15s;' +
                        (active ? 'background:'+activeBg+';color:#fff;box-shadow:0 2px 8px rgba(79,70,229,0.4);' :
                        'background:'+(isDark?'#2a2a3e':'#f3f4f6')+';color:'+fg+';') +
                        '">'+d+'초</button>';
                }).join('') + '</div>' +

                '<label style="display:block;font-size:12px;font-weight:600;color:'+sub+';margin-bottom:8px;">🖥️ 해상도</label>' +
                '<div id="vmod-res" style="display:flex;gap:6px;margin-bottom:14px;">' +
                cfg.resolutions.map(function(r) {
                    var active = r.label === selectedRes;
                    return '<button data-res="'+r.label+'" style="flex:1;padding:8px 4px;border-radius:8px;border:none;' +
                        'font-size:12px;font-weight:600;cursor:pointer;transition:all 0.15s;' +
                        (active ? 'background:'+activeBg+';color:#fff;box-shadow:0 2px 8px rgba(79,70,229,0.4);' :
                        'background:'+(isDark?'#2a2a3e':'#f3f4f6')+';color:'+fg+';') +
                        '"><div>'+r.label+'</div><div style="font-size:10px;opacity:0.7;">'+r.w+'×'+r.h+'</div></button>';
                }).join('') + '</div>' +

                '<div style="padding:12px;border-radius:10px;text-align:center;margin-bottom:16px;' +
                'background:'+(isDark?'rgba(79,70,229,0.15)':'#eef2ff')+';">' +
                '<span style="font-size:12px;color:'+sub+';">예상 소요시간: </span>' +
                '<span style="font-size:15px;font-weight:700;color:#4f46e5;">~'+est+'</span></div>' +

                '<button id="vmod-submit" style="width:100%;padding:14px;border:none;border-radius:12px;' +
                'font-size:14px;font-weight:700;color:#fff;cursor:pointer;' +
                'background:linear-gradient(135deg,#4f46e5,#7c3aed);' +
                'box-shadow:0 4px 16px rgba(79,70,229,0.3);transition:all 0.15s;">🎬 영상 생성하기</button>' +

                '<button id="vmod-cancel" style="width:100%;margin-top:8px;padding:10px;border:none;' +
                'border-radius:10px;background:transparent;font-size:13px;color:'+sub+';cursor:pointer;">닫기</button>';

            // Restore saved prompt
            var promptEl = document.getElementById('vmod-prompt');
            if (promptEl && savedPrompt) { promptEl.value = savedPrompt; }

            // Duration buttons
            card.querySelectorAll('#vmod-dur button').forEach(function(btn) {
                btn.onclick = function() {
                    selectedDur = parseInt(btn.dataset.dur);
                    renderCard();
                    var p = document.getElementById('vmod-prompt');
                    if (p) p.focus();
                };
            });

            // Resolution buttons
            card.querySelectorAll('#vmod-res button').forEach(function(btn) {
                btn.onclick = function() {
                    selectedRes = btn.dataset.res;
                    renderCard();
                    var p = document.getElementById('vmod-prompt');
                    if (p) p.focus();
                };
            });

            // Submit
            var submitBtn = card.querySelector('#vmod-submit');
            if (submitBtn) {
                submitBtn.onclick = function() {
                    var prompt = (document.getElementById('vmod-prompt') || {}).value || '';
                    if (!prompt.trim()) { alert('프롬프트를 입력하세요.'); return; }
                    var tag = '[' + selectedDur + '초 ' + selectedRes + ']';
                    submitMessage(tag + ' ' + prompt.trim());
                };
            }

            // Cancel
            var cancelBtn = card.querySelector('#vmod-cancel');
            if (cancelBtn) cancelBtn.onclick = dismissModal;
        }

        renderCard();
        overlay.appendChild(card);
        overlay.addEventListener('click', function(e) { if (e.target === overlay) dismissModal(); });
        document.body.appendChild(overlay);

        requestAnimationFrame(function() {
            overlay.style.opacity = '1';
            card.style.transform = 'translateY(0) scale(1)';
            setTimeout(function() {
                var p = document.getElementById('vmod-prompt');
                if (p) p.focus();
            }, 200);
        });
    }

    function submitMessage(msg) {
        closeModal();
        _modalDismissed = true;
        try { sessionStorage.setItem('video_modal_dismissed', Date.now().toString()); } catch(e) {}

        // Tiptap ProseMirror 에디터 or textarea 찾기
        var pm = document.querySelector('#chat-input .ProseMirror[contenteditable="true"]')
              || document.querySelector('.ProseMirror[contenteditable="true"]');
        var ta = !pm && (document.querySelector('textarea[placeholder]') || document.querySelector('textarea'));

        if (!pm && !ta) { console.warn('[VideoModal] input not found'); return; }

        if (pm) {
            pm.focus();
            // ProseMirror에 paste 이벤트로 텍스트 삽입 (내부 상태 동기화)
            var dt = new DataTransfer();
            dt.setData('text/plain', msg);
            pm.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        } else {
            ta.focus();
            var setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
            setter.call(ta, msg);
            ta.dispatchEvent(new Event('input', { bubbles: true }));
        }

        // 전송: Tiptap 상태 업데이트 후 버튼 활성화 대기
        var _sendAttempts = 0;
        var _trySend = function() {
            var btn = document.getElementById('send-message-button');
            if (btn && !btn.disabled) { btn.click(); return; }
            if (++_sendAttempts < 15) { setTimeout(_trySend, 200); return; }
            // fallback: form submit
            var el = pm || ta;
            var form = el ? el.closest('form') : null;
            if (form) { try { form.requestSubmit(); } catch(e) {} }
        };
        setTimeout(_trySend, 300);
    }

    function closeModal() {
        var m = document.getElementById(MODAL_ID);
        if (m) { m.style.opacity = '0'; setTimeout(function() { if (m.parentNode) m.remove(); }, 200); }
    }

    function dismissModal() { closeModal(); _modalDismissed = true; }

    /* ── 모델 변경 감시 ── */
    function poll() {
        var curUrl = location.href;
        if (curUrl !== _lastUrl) {
            _lastUrl = curUrl;
            // Don't reset dismissed if recently submitted (within 30 seconds)
            try {
                var ts = parseInt(sessionStorage.getItem('video_modal_dismissed') || '0');
                if (Date.now() - ts < 30000) { _modalDismissed = true; }
                else { _modalDismissed = false; }
            } catch(e) { _modalDismissed = false; }
        }

        var name = getSelectedModelName();
        if (name !== _prevModel) { _modalDismissed = false; _prevModel = name; }

        if (_modalDismissed || chatHasMessages()) return;
        if (document.getElementById(MODAL_ID)) return;

        var type = getModelType(name);
        if (type && !_modalDismissed) {
            showModal(type);
        }
    }

    setInterval(poll, 800);
})();
/*video_generation_modal_end*/
