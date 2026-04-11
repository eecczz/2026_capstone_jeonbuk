
/*hwp_template_selector_start*/
(function() {
    'use strict';

    var HWP_KEYWORD = 'HWP 파일 생성';
    var MODAL_ID = 'hwp-tpl-modal';
    var _prevModel = '';
    var _modalDismissed = false;
    var _lastUrl = location.href;

    var TEMPLATES = [
        { id: 'default', name: '기본 양식', desc: '도청 표준 행정 문서 양식', icon: '📄' },
        { id: 'v2', name: 'V2 양식', desc: '동적 섹션 구성 양식', icon: '📋' }
    ];

    /* ── 현재 선택된 모델 이름 가져오기 (안정적) ── */
    function getSelectedModelName() {
        // 1순위: OWI 상단 모델 셀렉터 버튼 (aria-label="모델 선택")
        var btn = document.querySelector('button[aria-label="모델 선택"]');
        if (btn) {
            var txt = btn.textContent || '';
            if (txt.length > 3 && txt.length < 200) return txt.trim();
        }
        // 2순위: nav 내 첫 번째 버튼 중 모델명 포함
        var navBtns = document.querySelectorAll('nav button');
        for (var i = 0; i < navBtns.length; i++) {
            var t = navBtns[i].textContent || '';
            if (t.includes('(') && t.includes(')') && t.length > 5 && t.length < 200) return t.trim();
        }
        return '';
    }

    /* ── 채팅에 이미 메시지가 있는지 확인 ── */
    function chatHasMessages() {
        // OWI 채팅 메시지 컨테이너 확인
        var msgs = document.querySelectorAll('[data-message-id], .message, [class*="chat-message"]');
        if (msgs.length > 0) return true;
        // 폴백: assistant 응답이 있는지 확인
        var assistantMsgs = document.querySelectorAll('[data-role="assistant"], .assistant-message');
        return assistantMsgs.length > 0;
    }

    /* ── 모달 생성 ── */
    function showModal() {
        if (document.getElementById(MODAL_ID)) return;

        var isDark = document.documentElement.classList.contains('dark');
        var bg = isDark ? '#1e1e2e' : '#ffffff';
        var fg = isDark ? '#e0e0e0' : '#1a1a1a';
        var sub = isDark ? '#999' : '#666';
        var border = isDark ? '#333' : '#e5e7eb';
        var cardBg = isDark ? '#2a2a3e' : '#fff';
        var hoverBg = isDark ? '#2d2d4a' : '#f0f7ff';

        var overlay = document.createElement('div');
        overlay.id = MODAL_ID;
        Object.assign(overlay.style, {
            position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh',
            background: 'rgba(0,0,0,0.5)', zIndex: '99999',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            backdropFilter: 'blur(3px)', opacity: '0', transition: 'opacity 0.2s'
        });

        var card = document.createElement('div');
        Object.assign(card.style, {
            background: bg, borderRadius: '16px', padding: '28px 28px 20px',
            maxWidth: '420px', width: '92%',
            boxShadow: '0 24px 80px rgba(0,0,0,0.4)',
            transform: 'translateY(10px) scale(0.97)',
            transition: 'transform 0.2s ease-out', color: fg
        });

        card.innerHTML =
            '<h3 style="margin:0 0 8px;font-size:18px;font-weight:700;">📝 HWP 양식 선택</h3>' +
            '<p style="margin:0 0 22px;font-size:14px;color:' + sub + ';line-height:1.5;">' +
            '현재의 대화를 바탕으로 HWP를 생성합니다.<br>양식을 선택해주세요.</p>' +
            '<div id="hwp-tpl-opts"></div>' +
            '<button id="hwp-tpl-cancel" style="margin-top:10px;width:100%;padding:10px;' +
            'border:1px solid ' + border + ';border-radius:8px;background:transparent;' +
            'cursor:pointer;font-size:14px;color:' + sub + ';">취소</button>';

        overlay.appendChild(card);

        // 배경 클릭 시 닫기
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) dismissModal();
        });

        document.body.appendChild(overlay);

        // 템플릿 버튼 추가
        var opts = document.getElementById('hwp-tpl-opts');
        TEMPLATES.forEach(function(tpl) {
            var btn = document.createElement('button');
            Object.assign(btn.style, {
                display: 'flex', alignItems: 'center', gap: '14px',
                width: '100%', padding: '18px 16px', marginBottom: '10px',
                border: '2px solid ' + border, borderRadius: '12px',
                background: cardBg, cursor: 'pointer', textAlign: 'left',
                transition: 'all 0.15s', color: fg, fontSize: '15px'
            });
            btn.innerHTML =
                '<span style="font-size:34px;line-height:1;">' + tpl.icon + '</span>' +
                '<div>' +
                '<div style="font-weight:600;font-size:15px;">' + tpl.name + '</div>' +
                '<div style="font-size:13px;color:' + sub + ';margin-top:3px;">' + tpl.desc + '</div>' +
                '</div>';
            btn.onmouseenter = function() { btn.style.borderColor = '#3b82f6'; btn.style.background = hoverBg; };
            btn.onmouseleave = function() { btn.style.borderColor = border; btn.style.background = cardBg; };
            btn.onclick = function() { pickTemplate(tpl); };
            opts.appendChild(btn);
        });

        document.getElementById('hwp-tpl-cancel').onclick = dismissModal;

        // 애니메이션
        requestAnimationFrame(function() {
            overlay.style.opacity = '1';
            card.style.transform = 'translateY(0) scale(1)';
        });
    }

    /* ── 선택 → 메시지 전송 ── */
    function pickTemplate(tpl) {
        closeModal();
        _modalDismissed = true;

        var msg = tpl.id === 'v2'
            ? 'V2 양식으로 HWP로 만들어줘'
            : '기본 양식으로 HWP로 만들어줘';

        // chat input 찾기 (TipTap contenteditable div 또는 legacy textarea)
        var ta = document.getElementById('chat-input')
              || document.querySelector('textarea[placeholder]')
              || document.querySelector('textarea');

        if (!ta) { console.warn('[HWP] input element not found'); return; }

        ta.focus();

        if (ta.tagName === 'TEXTAREA') {
            // legacy textarea 방식
            var setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
            setter.call(ta, msg);
            ta.dispatchEvent(new Event('input', { bubbles: true }));
        } else {
            // TipTap/ProseMirror contenteditable div
            var sel = window.getSelection();
            sel.selectAllChildren(ta);
            sel.deleteFromDocument();
            document.execCommand('insertText', false, msg);
        }

        // 전송
        setTimeout(function() {
            var form = ta.closest('form');
            if (form) {
                try { form.requestSubmit(); return; } catch(e) {}
            }
            ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
        }, 300);
    }

    function closeModal() {
        var m = document.getElementById(MODAL_ID);
        if (m) {
            m.style.opacity = '0';
            setTimeout(function() { if (m.parentNode) m.remove(); }, 200);
        }
    }

    function dismissModal() {
        closeModal();
        _modalDismissed = true;
    }

    /* ── 모델 변경 감지 루프 ── */
    setInterval(function() {
        var currentModel = getSelectedModelName();
        if (!currentModel) return;

        // URL 변경 감지 (새 채팅으로 이동 시 dismissed 리셋)
        var currentUrl = location.href;
        if (currentUrl !== _lastUrl) {
            _lastUrl = currentUrl;
            _modalDismissed = false;
            _prevModel = '';
        }

        var isHWP = currentModel.includes(HWP_KEYWORD);
        var wasHWP = _prevModel.includes(HWP_KEYWORD);

        // 다른 모델 → HWP 모델로 전환됨
        if (isHWP && !wasHWP && _prevModel !== '') {
            // 이미 닫은 적 있으면 재표시 안 함 (탭 전환 깜빡임 방지)
            if (_modalDismissed) {
                _prevModel = currentModel;
                return;
            }
            // 이미 대화 메시지가 있으면 모달 생략 (직전 대화가 HWP 관련)
            if (chatHasMessages()) {
                _modalDismissed = true;
                _prevModel = currentModel;
                return;
            }
            setTimeout(function() {
                if (!_modalDismissed && !document.getElementById(MODAL_ID)) {
                    showModal();
                }
            }, 500);
        }

        // HWP → 다른 모델로 전환 시 모달 닫기 (dismissed 리셋 안 함)
        if (!isHWP && wasHWP) {
            closeModal();
        }

        _prevModel = currentModel;
    }, 800);
})();
/*hwp_template_selector_end*/

/*login_title_override_start*/
(function() {
    'use strict';
    if (location.pathname !== '/auth') return;
    function fixTitle() {
        var el = document.querySelector('form.login-enhance-target .text-2xl.font-medium');
        if (!el) return;
        if (el.dataset.titleFixed) return;
        el.textContent = '전북도 생성형 AI 로그인';
        el.style.color = '#60a5fa';
        el.dataset.titleFixed = 'true';
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', fixTitle);
    } else {
        fixTitle();
    }
    var obs = new MutationObserver(fixTitle);
    obs.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(function() { obs.disconnect(); }, 10000);
})();
/*login_title_override_end*/

/*visualizations_loading_state_start*/

(function() {
    'use strict';

    // Constants
    const GENERATIVE_IMAGE_PATH = '/visualization/generative-image.jpg';
    const CHART_PATH = '/visualization/chart.png';
    const DEFAULT_ASPECT_RATIO = 16/9;
    // Chart defaults from backend variables
    const CHART_DEFAULT_WIDTH = 800;
    const CHART_DEFAULT_HEIGHT = 400;
    const CHART_SCALE_FACTOR = 5;
    const CHART_PADDING = 10;

    // === Element Detection ===
    function isWithinChatSection(img) {
        let element = img;
        while (element && element.parentElement) {
            if (element.tagName === 'SECTION' &&
                element.getAttribute('aria-labelledby') === 'chat-conversation') {
                return true;
            }
            element = element.parentElement;
        }
        return false;
    }

    // === DOM Operations ===
    function canProcessImage(img) {
        return !img.dataset.vizProcessed &&
               img.tagName === 'IMG' &&
               isWithinChatSection(img) &&
               (img.src?.includes(GENERATIVE_IMAGE_PATH) || img.src?.includes(CHART_PATH));
    }

    function markImageProcessed(img) {
        img.dataset.vizProcessed = 'true';
    }

    function removeWfitClasses(img) {
        let container = img.parentElement;
        let steps = 0;
        const maxSteps = 3;

        while (container && container.parentElement && steps < maxSteps) {
            if (container.classList.contains('w-fit')) {
                container.classList.remove('w-fit');
                break;
            }
            container = container.parentElement;
            steps++;
        }
    }

    // === Frontend Chart Spec Extraction ===
    function getVlSpecDict(imageUrl) {
        try {
            const url = new URL(imageUrl);
            const specParam = url.searchParams.get('spec');
            return specParam ? JSON.parse(decodeURIComponent(specParam)) : {};
        } catch (error) {
            console.warn('Failed to parse VL spec, using empty object:', error);
            return {};
        }
    }

    // === Frontend Chart Dimension Extraction ===
    function getChartDimension(vlSpecDict, dimension, defaultValue) {
        try {
            const value = vlSpecDict[dimension] || defaultValue;
            return parseFloat(value) || defaultValue;
        } catch (error) {
            return defaultValue;
        }
    }

    // === Frontend Chart Aspect Ratio Calculation ===
    function calculateChartAspectRatio(imageUrl) {
        // Parse VL spec from URL
        const vlSpecDict = getVlSpecDict(imageUrl);
        
        // Extract width and height using the helper method
        const width = getChartDimension(vlSpecDict, 'width', CHART_DEFAULT_WIDTH);
        const height = getChartDimension(vlSpecDict, 'height', CHART_DEFAULT_HEIGHT);
        
        // Return the aspect ratio of predicted final dimensions
        // Final dimensions: (width + padding*2) * scale_factor
        return ((width + (CHART_PADDING * 2)) * CHART_SCALE_FACTOR) / ((height + (CHART_PADDING * 2)) * CHART_SCALE_FACTOR);
    }

    // === Available Aspect Map ===
    // Dynamically injected by backend when filter is invoked
    // Map from aspect ratio names to [width, height] arrays
    const AVAILABLE_ASPECT_MAP = {"16:9": [1792, 1024]};


    // === Frontend Aspect Ratio Prediction ===
    function parseAspectRatio(aspectStr) {
        try {
            // Single regex to split on either ":" or "x"
            const parts = aspectStr.toLowerCase().split(/[:x]/);
            if (parts.length === 2) {
                const width = parseFloat(parts[0]);
                const height = parseFloat(parts[1]);
                if (width > 0 && height > 0) {
                    return [width, height];
                }
            }
        } catch (error) {
            // Parsing failed
        }
        return null;
    }

    function calculateGenerativeImageAspectRatio(imageUrl) {
        try {
            const url = new URL(imageUrl);
            const aspectRatioParam = url.searchParams.get('aspect_ratio') || '16:9';
            
            const targetRatio = parseAspectRatio(aspectRatioParam);
            if (!targetRatio || !Object.keys(AVAILABLE_ASPECT_MAP).length) return 16 / 9;
            
            const [targetWidth, targetHeight] = targetRatio;
            const targetValue = targetWidth / targetHeight;
            
            let bestDistance = Infinity;
            let bestDimensions = null;
            
            // Find closest supported aspect ratio using actual dimensions
            for (const [ratioName, dimensions] of Object.entries(AVAILABLE_ASPECT_MAP)) {
                const [width, height] = dimensions;
                const ratioValue = width / height;
                const distance = Math.abs(targetValue - ratioValue);
                if (distance < bestDistance) {
                    bestDistance = distance;
                    bestDimensions = dimensions;
                }
            }
            
            return bestDimensions ? bestDimensions[0] / bestDimensions[1] : 16 / 9;
        } catch (error) {
            console.warn('Failed to predict generative image aspect ratio:', error);
            return 16 / 9;
        }
    }

    function calculateAspectRatio(imageUrl) {
        try {
            if (imageUrl.includes(CHART_PATH)) {
                const ratio = calculateChartAspectRatio(imageUrl);
                console.log('Chart aspect ratio calculated:', ratio);
                return ratio;
            } else if (imageUrl.includes(GENERATIVE_IMAGE_PATH)) {
                const ratio = calculateGenerativeImageAspectRatio(imageUrl);
                console.log('Generative image aspect ratio predicted:', ratio);
                return ratio;
            } else {
                console.log('Unknown image type, using default aspect ratio');
                return DEFAULT_ASPECT_RATIO;
            }
        } catch (error) {
            console.warn('Failed to calculate aspect ratio, using default:', error);
            return DEFAULT_ASPECT_RATIO;
        }
    }

    // === Styling ===
    function createBackgroundStyle() {
        // Create a style element for the animation
        const styleId = 'viz-loading-animation-style';
        let styleEl = document.getElementById(styleId);

        if (!styleEl) {
            styleEl = document.createElement('style');
            styleEl.id = styleId;
            styleEl.textContent = `
                @keyframes viz-loading-pulse {
                    0%, 100% {
                        background-color: #d1d5db;
                    }
                    50% {
                        background-color: #f3f4f6;
                    }
                }

                .viz-loading-placeholder {
                    animation: viz-loading-pulse 2s ease-in-out infinite;
                }

                .viz-error-state {
                    background-color: #fca5a5;
                    animation: none;
                }
            `;
            document.head.appendChild(styleEl);
        }
    }

    function createPlaceholder(aspectRatio) {
        createBackgroundStyle();

        const wrapper = document.createElement('div');
        wrapper.classList.add('rounded-lg', 'viz-loading-placeholder');

        Object.assign(wrapper.style, {
            display: 'block',
            width: '100%',
            position: 'relative',
            overflow: 'hidden',
            paddingBottom: `${(1 / aspectRatio) * 100}%`,
            height: '0'
        });

        return wrapper;
    }

    function createImageClone(imgElement) {
        const imgClone = imgElement.cloneNode(true);
        imgClone.classList.remove('rounded-lg');
        imgClone.style.margin = '0';

        Object.assign(imgClone.style, {
            position: 'absolute',
            top: '0',
            left: '0',
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            opacity: '0',
            transition: 'opacity 0.1s ease-in-out'
        });

        return imgClone;
    }

    function setupImageLoadHandler(imgClone) {
        const handleLoad = () => {
            imgClone.style.opacity = '1';
            // Stop the loading animation
            stopLoadingAnimation(imgClone.parentElement);
        };

        const handleError = () => {
            // Apply error state with red background
            applyErrorState(imgClone.parentElement);
        };

        imgClone.onload = handleLoad;
        imgClone.onerror = handleError;

        // Handle cached images
        if (imgClone.complete && imgClone.naturalHeight !== 0) {
            handleLoad();
        }
    }

    function stopLoadingAnimation(wrapper) {
        if (wrapper && wrapper.classList.contains('viz-loading-placeholder')) {
            wrapper.style.animation = 'none';
            wrapper.classList.remove('viz-loading-placeholder');
        }
    }

    function applyErrorState(wrapper) {
        if (wrapper) {
            // Stop the loading animation
            wrapper.style.animation = 'none';
            wrapper.classList.remove('viz-loading-placeholder');
            
            // Apply error state with red background
            wrapper.classList.add('viz-error-state');
        }
    }

    // === Main Processing Functions ===
    async function processImage(imgElement) {
        if (!canProcessImage(imgElement)) {
            return;
        }

        markImageProcessed(imgElement);

        try {
            let aspectRatio;
            
            if (imgElement.src.includes(CHART_PATH)) {
                // Calculate chart aspect ratio immediately (no network call)
                aspectRatio = calculateChartAspectRatio(imgElement.src);
                console.log('Chart aspect ratio calculated frontend:', aspectRatio);
            } else if (imgElement.src.includes(GENERATIVE_IMAGE_PATH)) {
                // Use local prediction for generative images (no per-image backend call)
                aspectRatio = calculateGenerativeImageAspectRatio(imgElement.src);
                console.log('Generative image aspect ratio predicted locally:', aspectRatio);
            } else {
                aspectRatio = DEFAULT_ASPECT_RATIO;
                console.log('Unknown image type, using default aspect ratio:', aspectRatio);
            }
            
            wrapImageWithPlaceholder(imgElement, aspectRatio);
        } catch (error) {
            console.warn('Failed to process loading state:', error);
            wrapImageWithPlaceholder(imgElement, DEFAULT_ASPECT_RATIO);
        }
    }

    function wrapImageWithPlaceholder(imgElement, aspectRatio) {
        const parent = imgElement.parentNode;
        if (!parent) {
            console.warn('Image has no parent element, cannot apply wrapper');
            return;
        }

        removeWfitClasses(imgElement);

        const wrapper = createPlaceholder(aspectRatio);
        const imgClone = createImageClone(imgElement);

        wrapper.appendChild(imgClone);
        parent.replaceChild(wrapper, imgElement);

        setupImageLoadHandler(imgClone);
    }

    // === Initialization ===
    function processExistingImages() {
        const images = document.querySelectorAll('img');
        images.forEach(processImage);
    }

    function setupMutationObserver() {
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        if (node.tagName === 'IMG') {
                            processImage(node);
                        }

                        const descendantImages = node.querySelectorAll?.('img');
                        if (descendantImages) {
                            descendantImages.forEach(processImage);
                        }
                    }
                });
            });
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    function initializeImageLoader() {
        processExistingImages();
        setupMutationObserver();
    }

    // === DOM Ready Check ===
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeImageLoader);
    } else {
        initializeImageLoader();
    }
})();

/*visualizations_loading_state_end*/






/*pw_change_overlay_start*/
(function() {
    'use strict';
    var PW_OVERLAY_ID = 'pw-change-overlay';
    var _done = false;

    function getToken() {
        try { return localStorage.getItem('token') || ''; } catch(e) { return ''; }
    }

    function checkAndShow() {
        if (_done) return true;
        if (location.pathname === '/auth') return false;
        var token = getToken();
        if (!token) return false;

        fetch('/api/v1/auths/', { headers: { 'Authorization': 'Bearer ' + token } })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data && data.info && data.info.must_change_pw) {
                showPwOverlay();
            } else {
                _done = true;
            }
        })
        .catch(function() {});
        return true;
    }

    function showPwOverlay() {
        if (document.getElementById(PW_OVERLAY_ID)) return;

        var isDark = document.documentElement.classList.contains('dark');
        var bg = isDark ? '#1e1e2e' : '#ffffff';
        var fg = isDark ? '#e0e0e0' : '#1a1a1a';
        var sub = isDark ? '#aaa' : '#555';
        var inputBg = isDark ? '#2a2a3e' : '#f9fafb';
        var inputBorder = isDark ? '#444' : '#d1d5db';

        var overlay = document.createElement('div');
        overlay.id = PW_OVERLAY_ID;
        Object.assign(overlay.style, {
            position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh',
            background: 'rgba(0,0,0,0.7)', zIndex: '999999',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            backdropFilter: 'blur(5px)', opacity: '0', transition: 'opacity 0.3s'
        });

        var card = document.createElement('div');
        Object.assign(card.style, {
            background: bg, borderRadius: '20px', padding: '36px 32px 28px',
            maxWidth: '440px', width: '90%', textAlign: 'center',
            boxShadow: '0 24px 80px rgba(0,0,0,0.5)',
            transform: 'translateY(12px) scale(0.96)',
            transition: 'transform 0.3s ease-out', color: fg
        });

        var inputStyle = 'width:100%;padding:12px 14px;border:1px solid ' + inputBorder +
            ';border-radius:10px;background:' + inputBg + ';color:' + fg +
            ';font-size:15px;margin-bottom:10px;box-sizing:border-box;outline:none;';

        card.style.position = 'relative';
        card.innerHTML =
            '<button id="pw-close-btn" style="position:absolute;top:12px;right:16px;' +
            'background:none;border:none;font-size:22px;cursor:pointer;color:' + sub + ';' +
            'width:32px;height:32px;display:flex;align-items:center;justify-content:center;' +
            'border-radius:8px;transition:background 0.15s;">&times;</button>' +
            '<div style="font-size:48px;margin-bottom:12px;">🔐</div>' +
            '<h2 style="margin:0 0 8px;font-size:22px;font-weight:700;">비밀번호 변경</h2>' +
            '<p style="margin:0 0 20px;font-size:14px;color:' + sub + ';line-height:1.5;">' +
            '초기 비밀번호를 사용 중입니다. 보안을 위해 변경해 주세요.</p>' +
            '<input id="pw-cur" type="text" value="12345678" readonly style="' + inputStyle + 'color:' + sub + ';cursor:default;">' +
            '<input id="pw-new" type="text" placeholder="새 비밀번호 (8자 이상)" style="' + inputStyle + '">' +
            '<input id="pw-confirm" type="text" placeholder="새 비밀번호 확인" style="' + inputStyle + '">' +
            '<div id="pw-msg" style="min-height:24px;font-size:13px;margin:4px 0 12px;"></div>' +
            '<button id="pw-submit-btn" style="width:100%;padding:14px;border:none;' +
            'border-radius:12px;background:linear-gradient(135deg,#3b82f6,#6366f1);' +
            'color:#fff;font-size:16px;font-weight:600;cursor:pointer;' +
            'transition:transform 0.1s,box-shadow 0.1s;box-shadow:0 4px 16px rgba(59,130,246,0.4);">' +
            '비밀번호 변경하기</button>';

        overlay.appendChild(card);
        document.body.appendChild(overlay);

        requestAnimationFrame(function() {
            overlay.style.opacity = '1';
            card.style.transform = 'translateY(0) scale(1)';
        });

        var msgEl = document.getElementById('pw-msg');
        var submitBtn = document.getElementById('pw-submit-btn');
        var closeBtn = document.getElementById('pw-close-btn');

        closeBtn.onmouseenter = function() { closeBtn.style.background = isDark ? '#333' : '#f3f4f6'; };
        closeBtn.onmouseleave = function() { closeBtn.style.background = 'none'; };
        closeBtn.onclick = function() {
            _done = true;
            overlay.style.opacity = '0';
            setTimeout(function() { if (overlay.parentNode) overlay.remove(); }, 300);
        };

        submitBtn.onmouseenter = function() { submitBtn.style.transform = 'scale(1.02)'; };
        submitBtn.onmouseleave = function() { submitBtn.style.transform = 'scale(1)'; };

        submitBtn.onclick = function() {
            var cur = document.getElementById('pw-cur').value;
            var nw = document.getElementById('pw-new').value;
            var cf = document.getElementById('pw-confirm').value;

            if (!cur) { msgEl.style.color = '#ef4444'; msgEl.textContent = '현재 비밀번호를 입력하세요.'; return; }
            if (nw.length < 8) { msgEl.style.color = '#ef4444'; msgEl.textContent = '새 비밀번호는 8자 이상이어야 합니다.'; return; }
            if (nw !== cf) { msgEl.style.color = '#ef4444'; msgEl.textContent = '새 비밀번호가 일치하지 않습니다.'; return; }
            if (cur === nw) { msgEl.style.color = '#ef4444'; msgEl.textContent = '현재 비밀번호와 다른 비밀번호를 입력하세요.'; return; }

            submitBtn.disabled = true;
            submitBtn.textContent = '변경 중...';
            msgEl.textContent = '';

            fetch('/api/v1/auths/update/password', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + getToken(), 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: cur, new_password: nw })
            })
            .then(function(r) {
                if (r.ok) return r.json();
                return r.json().then(function(d) { throw new Error(d.detail || '변경 실패'); });
            })
            .then(function() {
                _done = true;
                msgEl.style.color = '#22c55e';
                msgEl.textContent = '비밀번호가 변경되었습니다!';
                submitBtn.textContent = '완료';
                submitBtn.style.background = 'linear-gradient(135deg,#22c55e,#16a34a)';
                setTimeout(function() {
                    overlay.style.opacity = '0';
                    setTimeout(function() { if (overlay.parentNode) overlay.remove(); }, 300);
                }, 1500);
            })
            .catch(function(e) {
                msgEl.style.color = '#ef4444';
                msgEl.textContent = e.message || '비밀번호 변경에 실패했습니다.';
                submitBtn.disabled = false;
                submitBtn.textContent = '비밀번호 변경하기';
            });
        };
    }

    var _pollId = setInterval(function() {
        if (checkAndShow()) clearInterval(_pollId);
    }, 2000);
    setTimeout(function() { clearInterval(_pollId); }, 30000);
})();
/*pw_change_overlay_end*/
/**
 * JBTP Citation Panel v7 - RAG 소스 뷰어
 * 변경사항 (v7):
 * - 패널 너비 700px → 넉넉한 뷰어 공간
 * - web_search 타입: metadata 각 항목을 개별 웹페이지로 표시 (제목+URL+본문)
 * - "domain +N more sources" 버튼 인터셉트 추가 (v6에서 누락)
 * - 도메인 매칭으로 관련 소스 하이라이트
 * - PDF: 기존 방식 유지, 크기만 확대
 * - 웹소스 클릭 시 새 탭 열기 + 본문 텍스트 표시
 */
(function() {
  'use strict';

  const PANEL_WIDTH = 900;

  // === State ===
  let panelVisible = false;
  let panel = null;
  let lastRenderedKey = '';
  let _cachedSources = [];
  let _lastChatId = '';
  const _pdfBlobCache = {};
  const _pdfDocCache = {};
  const _pageLabelMap = {};  // fileId -> { label -> physicalPage }
  let _highlightDocText = '';  // 문맥 매칭된 문서 청크 (PDF 하이라이팅용)
  let _highlightRetried = false;

  // === pdf.js Setup (자동 CDN 로딩) ===
  const PDFJS_VER = '5.4.149';
  const PDFJS_CDN = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@' + PDFJS_VER;
  let _pdfjsReady = null;

  function ensurePdfJs() {
    if (_pdfjsReady) return _pdfjsReady;
    _pdfjsReady = (async () => {
      // 1) OWI가 이미 로딩한 pdfjsLib 재사용 (Baj9Iijm.js → globalThis.pdfjsLib)
      let lib = globalThis.pdfjsLib || window.pdfjsLib;
      if (lib && typeof lib.getDocument === 'function') {
        console.log('[JBTP] Reusing existing pdfjsLib v' + (lib.version || '?'));
      } else {
        // 2) 없으면 CDN에서 dynamic import (ESM)
        console.log('[JBTP] pdfjsLib not found, loading from CDN v' + PDFJS_VER);
        const cdnUrl = PDFJS_CDN + '/build/pdf.min.mjs';
        lib = await import(/* webpackIgnore: true */ cdnUrl);
        globalThis.pdfjsLib = lib;
        window.pdfjsLib = lib;
        console.log('[JBTP] pdfjsLib loaded from CDN, version:', lib.version || PDFJS_VER);
      }
      // Worker 설정
      if (!lib.GlobalWorkerOptions.workerSrc) {
        const workerUrl = PDFJS_CDN + '/build/pdf.worker.min.mjs';
        try {
          const r = await fetch(workerUrl);
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const blob = await r.blob();
          lib.GlobalWorkerOptions.workerSrc = URL.createObjectURL(blob);
          console.log('[JBTP] pdf.js worker loaded via blob URL');
        } catch(e) {
          console.warn('[JBTP] Worker blob failed, using direct URL:', e.message);
          lib.GlobalWorkerOptions.workerSrc = workerUrl;
        }
      }
    })();
    // 실패 시 다음 클릭에서 재시도할 수 있도록 리셋
    _pdfjsReady.catch(() => { _pdfjsReady = null; });
    return _pdfjsReady;
  }

  function getToken() {
    try { const t = localStorage.getItem('token'); return t ? t.replace(/"/g, '') : ''; }
    catch(e) { return ''; }
  }

  // === Fetch Intercept ===
  let _realFetch = null;
  let _ourFetch = null;

  function installFetchIntercept() {
    _realFetch = window.fetch;
    _ourFetch = wrapFetch(_realFetch);
    window.fetch = _ourFetch;
    setInterval(() => {
      if (window.fetch !== _ourFetch) {
        _realFetch = window.fetch;
        _ourFetch = wrapFetch(_realFetch);
        window.fetch = _ourFetch;
      }
    }, 2000);
  }

  function wrapFetch(origFetch) {
    return async function(...args) {
      const resp = await origFetch.apply(this, args);
      const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
      const method = (args[1]?.method || 'GET').toUpperCase();

      // GET chat data — 소스 캐싱
      if (/\/api\/v1\/chats\/[0-9a-f-]{36}$/.test(url)) {
        try {
          const clone = resp.clone();
          const data = await clone.json();
          extractSourcesFromChat(data);
        } catch(e) {}
      }

      // POST chat completions — SSE 스트림에서 소스 실시간 캡처
      if (method === 'POST' && /\/api\/(v1\/)?chat\/completions/.test(url)) {
        const ct = resp.headers.get('content-type') || '';
        console.log('[JBTP] Chat completions intercepted, ct:', ct, 'body:', !!resp.body);
        if (ct.includes('text/event-stream') && resp.body) {
          try {
            const [s1, s2] = resp.body.tee();
            _readStreamForSources(s2);
            console.log('[JBTP] Stream tee OK, reading sources...');
            return new Response(s1, { status: resp.status, statusText: resp.statusText, headers: resp.headers });
          } catch(e) { console.warn('[JBTP] Stream tee failed:', e); }
        } else if (resp.body) {
          // SSE가 아닌 경우에도 응답에서 소스 추출 시도
          console.log('[JBTP] Non-SSE response, trying clone...');
          try {
            const clone = resp.clone();
            clone.json().then(data => {
              if (data && data.sources && Array.isArray(data.sources)) {
                _cachedSources = data.sources;
                console.log('[JBTP] Sources from JSON response:', data.sources.length);
              }
            }).catch(() => {});
          } catch(e) {}
        }
      }

      return resp;
    };
  }

  // === SSE 스트림에서 소스 실시간 캡처 ===
  async function _readStreamForSources(stream) {
    try {
      const reader = stream.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (!raw || raw === '[DONE]') continue;
          try {
            const evt = JSON.parse(raw);
            if (evt.type === 'source' && evt.data) {
              _cachedSources.push(evt.data);
              console.log('[JBTP] Source captured during stream:', (evt.data.source || {}).name);
            }
            if (evt.sources && Array.isArray(evt.sources)) {
              _cachedSources = evt.sources;
              console.log('[JBTP] Sources captured during stream:', evt.sources.length);
            }
          } catch(e) {}
        }
      }
    } catch(e) { console.warn('[JBTP] Stream read error:', e); }
  }

  // === 클릭된 배지 주변 텍스트 추출 (페이지 매칭용) ===
  function _getContextText(btn) {
    let text = '';
    let node = btn.previousSibling;
    while (node && text.length < 300) {
      text = (node.textContent || '') + text;
      node = node.previousSibling;
    }
    return text.replace(/\s+/g, ' ').trim().slice(-200);
  }

  // === 문맥 텍스트로 매칭 페이지 찾기 ===
  function _findMatchingPage(item, contextText) {
    if (!contextText || !item.docs || item.docs.length === 0) return null;
    // N-gram 오버랩: LLM이 재구성한 텍스트도 원본 청크와 3-char gram 겹침으로 매칭
    function ngrams(text) {
      const c = text.replace(/[\s,.\(\)\/·•\-□\n「」『』""''：:；;~\d]/g, '').toLowerCase();
      const s = new Set();
      for (let i = 0; i <= c.length - 3; i++) s.add(c.slice(i, i + 3));
      return s;
    }
    const ctxG = ngrams(contextText);
    if (ctxG.size === 0) return null;
    let bestIdx = -1, bestScore = 0;
    for (let i = 0; i < item.docs.length; i++) {
      if (!item.docs[i]) continue;
      const dg = ngrams(item.docs[i]);
      let sc = 0;
      for (const g of ctxG) { if (dg.has(g)) sc++; }
      if (sc > bestScore) { bestScore = sc; bestIdx = i; }
    }
    return bestIdx >= 0 && bestScore >= 3 ? bestIdx : null;
  }

  function extractSourcesFromChat(data) {
    const chatId = data?.chat?.id || '';
    const messages = data?.chat?.messages;
    if (!messages) return;

    // 채팅이 바뀌면 캐시 클리어
    if (chatId && chatId !== _lastChatId) {
      _cachedSources = [];
      lastRenderedKey = '';
      _lastChatId = chatId;
    }

    let allSources = [];
    // messages 배열에서 소스 추출
    const msgArr = Array.isArray(messages) ? messages : Object.values(messages);
    for (const msg of msgArr) {
      if (msg.role === 'assistant' && msg.sources && msg.sources.length > 0) {
        allSources = allSources.concat(msg.sources);
      }
    }
    // messages 배열에 소스 없으면 history.messages에서 fallback
    if (allSources.length === 0) {
      const histMsgs = data?.chat?.history?.messages;
      if (histMsgs && typeof histMsgs === 'object') {
        for (const msg of Object.values(histMsgs)) {
          if (msg.role === 'assistant' && msg.sources && msg.sources.length > 0) {
            allSources = allSources.concat(msg.sources);
          }
        }
      }
    }
    if (allSources.length > 0) {
      _cachedSources = allSources;
      const key = chatId + '::' + allSources.map(s => (s.source || {}).name + '|' + (s.metadata || []).length).join('||');
      if (key !== lastRenderedKey) {
        lastRenderedKey = key;
      }
    }
  }

  // === 소스를 "표시 가능한 항목"으로 변환 ===
  function flattenSources(sources) {
    const items = [];
    for (const src of sources) {
      const srcInfo = src.source || {};
      const srcType = srcInfo.type || '';
      const metas = src.metadata || [];
      const docs = src.document || [];

      if (srcType === 'web_search') {
        // 웹 검색: metadata 각 항목을 개별 웹페이지로 표시
        for (let mi = 0; mi < metas.length; mi++) {
          const m = metas[mi];
          const pageUrl = m.source || '';
          const title = m.title || extractDomain(pageUrl) || 'Web Page';
          const desc = m.description || '';
          const docText = docs[mi] || '';
          items.push({
            type: 'web',
            title: title,
            url: pageUrl,
            domain: extractDomain(pageUrl),
            description: desc,
            textContent: docText,
            groupName: srcInfo.name || ''
          });
        }
      } else {
        // PDF/collection: 기존 방식 유지
        const rawName = srcInfo.name || 'Unknown';
        const displayName = rawName.replace(/^\[p\.[^\]]*\]\s*/, '');
        const pageMap = {};
        let fallbackFileId = null;
        metas.forEach((m, mi) => {
          if (!fallbackFileId && m.file_id) fallbackFileId = m.file_id;
          const lbl = m.page_label || (m.page != null ? String(Number(m.page) + 1) : null);
          if (!lbl || pageMap[lbl]) return;
          pageMap[lbl] = {
            page: m.page,
            pageLabel: lbl,
            fileId: m.file_id,
            totalPages: m.total_pages,
            docIdx: mi
          };
        });
        items.push({
          type: 'pdf',
          title: displayName,
          rawName: rawName,
          pageMap: pageMap,
          fallbackFileId: fallbackFileId,
          docs: docs,
          files: srcInfo.files || []
        });
      }
    }
    return items;
  }

  function extractDomain(url) {
    if (!url) return '';
    try {
      const u = new URL(url);
      return u.hostname.replace(/^www\./, '');
    } catch(e) {
      const m = url.match(/\/\/(?:www\.)?([^\/]+)/);
      return m ? m[1] : url;
    }
  }

  // === Render ===
  function renderSources(sources, matchDomain, contextText) {
    if (!sources?.length) return;
    if (!panel) createPanel();
    const list = document.getElementById('jbtp-source-list');
    if (!list) return;
    list.innerHTML = '';

    const items = flattenSources(sources);
    console.log('[JBTP] Rendering items:', items.length, 'matchDomain:', matchDomain);

    let firstMatchEl = null;
    let matchCount = 0;

    items.forEach((item, idx) => {
      if (item.type === 'web') {
        const el = renderWebItem(item, idx, matchDomain);
        list.appendChild(el);
        if (matchDomain && item.domain && item.domain.includes(matchDomain)) {
          el.classList.add('jbtp-highlight');
          matchCount++;
          if (!firstMatchEl) firstMatchEl = el;
        }
      } else {
        const el = renderPdfItem(item, idx, matchDomain);
        list.appendChild(el);
        if (matchDomain && (item.rawName || item.title || '').toLowerCase().includes(matchDomain.toLowerCase())) {
          el.classList.add('jbtp-highlight');
          if (!firstMatchEl) firstMatchEl = el;
        }
      }
    });

    // 매칭된 항목으로 스크롤 + 문맥 기반 페이지 선택
    if (firstMatchEl) {
      setTimeout(() => {
        firstMatchEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        // 웹소스는 본문 보기
        const textBtn = firstMatchEl.querySelector('.jbtp-web-text-btn');
        if (textBtn) { textBtn.click(); return; }
        // PDF: 문맥 텍스트로 올바른 페이지 찾기
        const matchItem = items.find(i => i.type === 'pdf');
        if (matchItem && contextText) {
          const docIdx = _findMatchingPage(matchItem, contextText);
          if (docIdx !== null) {
            _highlightDocText = matchItem.docs[docIdx] || '';
            _highlightRetried = false;
            const sortedPages = Object.keys(matchItem.pageMap).sort((a, b) => parseInt(a) - parseInt(b));
            for (const pk of sortedPages) {
              if (matchItem.pageMap[pk].docIdx === docIdx) {
                const btns = firstMatchEl.querySelectorAll('.jbtp-page-btn');
                const targetBtn = Array.from(btns).find(b => b.textContent === 'p.' + pk);
                if (targetBtn) {
                  console.log('[JBTP] Context matched page:', pk, 'docIdx:', docIdx);
                  targetBtn.click();
                  return;
                }
              }
            }
          }
        }
        // 폴백: 첫 번째 페이지
        const pageBtn = firstMatchEl.querySelector('.jbtp-page-btn');
        if (pageBtn) pageBtn.click();
      }, 100);
    }
  }

  function renderWebItem(item, idx, matchDomain) {
    const el = document.createElement('div');
    el.className = 'jbtp-source-item jbtp-web-source';

    const hdr = document.createElement('div');
    hdr.className = 'jbtp-source-header';
    const favicon = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(item.domain) + '&sz=16';
    hdr.innerHTML =
      '<img src="' + favicon + '" class="jbtp-favicon" onerror="this.style.display=\'none\'">' +
      '<div class="jbtp-web-info">' +
        '<span class="jbtp-web-title">' + escHtml(item.title) + '</span>' +
        '<span class="jbtp-web-url">' + escHtml(item.domain) + '</span>' +
      '</div>';

    // 클릭 시 본문 표시
    hdr.onclick = () => showWebContent(item);
    hdr.style.cursor = 'pointer';
    el.appendChild(hdr);

    // 빠른 액션 버튼
    const actions = document.createElement('div');
    actions.className = 'jbtp-web-actions';
    if (item.url) {
      const openBtn = document.createElement('a');
      openBtn.className = 'jbtp-web-open';
      openBtn.href = item.url;
      openBtn.target = '_blank';
      openBtn.rel = 'noopener';
      openBtn.textContent = '원본 페이지 열기 ↗';
      openBtn.onclick = (e) => e.stopPropagation();
      actions.appendChild(openBtn);
    }
    const textBtn = document.createElement('button');
    textBtn.className = 'jbtp-web-text-btn';
    textBtn.textContent = '본문 보기';
    textBtn.onclick = (e) => {
      e.stopPropagation();
      showWebContent(item);
    };
    actions.appendChild(textBtn);
    el.appendChild(actions);

    return el;
  }

  function showWebContent(item) {
    const viewer = document.getElementById('jbtp-viewer');
    if (!viewer) return;

    const cleanText = stripHtml(item.textContent || '');
    const desc = item.description ? '<div class="jbtp-web-desc">' + escHtml(item.description) + '</div>' : '';

    viewer.innerHTML =
      '<div class="jbtp-viewer-header">' +
        '<span>' + escHtml(item.title) + '</span>' +
        (item.url ? '<a href="' + escHtml(item.url) + '" target="_blank" rel="noopener" class="jbtp-open-link" title="원본 페이지 열기">↗</a>' : '') +
      '</div>' +
      '<div class="jbtp-web-viewer">' +
        '<div class="jbtp-web-meta">' +
          '<span class="jbtp-web-domain-badge">' + escHtml(item.domain) + '</span>' +
          (item.url ? '<a href="' + escHtml(item.url) + '" target="_blank" rel="noopener" class="jbtp-web-fullurl">' + escHtml(item.url) + '</a>' : '') +
        '</div>' +
        desc +
        '<div class="jbtp-web-body">' + escHtml(cleanText || '본문 내용 없음') + '</div>' +
      '</div>';
  }

  function stripHtml(html) {
    if (!html) return '';
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    // 스크립트/스타일 제거
    tmp.querySelectorAll('script,style,nav,header,footer').forEach(el => el.remove());
    return (tmp.textContent || tmp.innerText || '').replace(/\s+/g, ' ').trim();
  }

  function renderPdfItem(item, idx, matchDomain) {
    const el = document.createElement('div');
    el.className = 'jbtp-source-item jbtp-pdf-source expanded';

    const hdr = document.createElement('div');
    hdr.className = 'jbtp-source-header';
    hdr.innerHTML =
      '<span class="jbtp-source-idx">[' + (idx + 1) + ']</span>' +
      '<span class="jbtp-source-name">' + escHtml(item.title) + '</span>';
    hdr.onclick = () => el.classList.toggle('expanded');
    el.appendChild(hdr);

    const pgList = document.createElement('div');
    pgList.className = 'jbtp-page-list';
    const sortedPages = Object.keys(item.pageMap).sort((a, b) => {
      const na = parseInt(a), nb = parseInt(b);
      return (isNaN(na) || isNaN(nb)) ? 0 : na - nb;
    });

    sortedPages.forEach(pk => {
      const info = item.pageMap[pk];
      const btn = document.createElement('button');
      btn.className = 'jbtp-page-btn';
      btn.textContent = 'p.' + pk;
      if (info.totalPages) btn.title = pk + ' / ' + info.totalPages + ' 페이지';
      btn.onclick = (e) => {
        e.stopPropagation();
        document.querySelectorAll('.jbtp-page-btn.active').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        _highlightDocText = item.docs[info.docIdx] || '';
        _highlightRetried = false;
        loadPdfPage(info.fileId, info.page, info.pageLabel, item.docs[info.docIdx]);
      };
      pgList.appendChild(btn);
    });

    if (sortedPages.length === 0) {
      const fid = item.fallbackFileId || (item.files?.[0]?.id);
      if (fid && item.docs && item.docs.filter(Boolean).length > 0) {
        // 페이지 메타데이터 없음 → 각 인용 구간별 버튼 (클릭 시 PDF 내 텍스트 검색)
        item.docs.forEach((doc, di) => {
          if (!doc) return;
          const btn = document.createElement('button');
          btn.className = 'jbtp-page-btn';
          btn.textContent = '인용 ' + (di + 1);
          btn.title = (doc || '').substring(0, 80) + '...';
          btn.onclick = async (e) => {
            e.stopPropagation();
            document.querySelectorAll('.jbtp-page-btn.active').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _highlightDocText = doc;
            _highlightRetried = false;
            const viewer = document.getElementById('jbtp-viewer');
            if (!viewer) return;
            viewer.innerHTML = '<div class="jbtp-viewer-header"><span>페이지 검색 중...</span></div><div class="jbtp-viewer-placeholder">PDF에서 인용 위치를 찾는 중...</div>';
            try {
              await ensurePdfJs();
              let pdfDoc = _pdfDocCache[fid];
              if (!pdfDoc) {
                const token = getToken();
                const fetchFn = _realFetch || fetch;
                const r = await fetchFn('/api/v1/files/' + fid + '/content', { headers: { 'Authorization': 'Bearer ' + token } });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const blob = await r.blob();
                const blobUrl = URL.createObjectURL(blob);
                _pdfBlobCache[fid] = blobUrl;
                pdfDoc = await window.pdfjsLib.getDocument(blobUrl).promise;
                _pdfDocCache[fid] = pdfDoc;
              }
              const foundPage = await _searchPdfForText(pdfDoc, doc);
              const targetPage = foundPage || 1;
              console.log('[JBTP] Text search: page', foundPage, 'for chunk', di);
              btn.textContent = 'p.' + targetPage;
              _renderPdfCanvas(viewer, pdfDoc, targetPage, fid);
            } catch(err) {
              console.error('[JBTP] PDF search error:', err);
              loadPdfPage(fid, 0, '1', doc);
            }
          };
          pgList.appendChild(btn);
        });
      } else if (fid) {
        const openBtn = document.createElement('button');
        openBtn.className = 'jbtp-page-btn';
        openBtn.textContent = 'PDF 열기';
        openBtn.onclick = (e) => {
          e.stopPropagation();
          document.querySelectorAll('.jbtp-page-btn.active').forEach(b => b.classList.remove('active'));
          openBtn.classList.add('active');
          loadPdfPage(fid, 0, '1', item.docs[0] || null);
        };
        pgList.appendChild(openBtn);
      }
    }

    el.appendChild(pgList);
    return el;
  }

  // === PDF 전체 텍스트 검색 (페이지 메타데이터 없을 때) ===
  async function _searchPdfForText(pdfDoc, searchText) {
    if (!searchText || searchText.length < 20) return null;
    const _hn = t => t.replace(/[\s\u0000\u00a0]+/g, '');
    const searchNorm = _hn(searchText);
    const anchorLen = Math.min(40, searchNorm.length);
    const anchors = [0, 0.25, 0.5, 0.75].map(r => {
      const off = Math.floor(r * Math.max(0, searchNorm.length - anchorLen));
      return searchNorm.slice(off, off + anchorLen);
    }).filter(a => a.length >= 10);
    if (anchors.length === 0) return null;

    // 1차: 매 20페이지마다 스캔하여 대략적 위치 파악
    const step = Math.max(1, Math.min(20, Math.floor(pdfDoc.numPages / 25)));
    let roughPage = null;
    for (let i = 1; i <= pdfDoc.numPages; i += step) {
      const pg = await pdfDoc.getPage(i);
      const tc = await pg.getTextContent();
      let text = '';
      for (const it of tc.items) { if (it.str) text += it.str; }
      const norm = _hn(text);
      for (const anchor of anchors) {
        if (norm.includes(anchor)) { roughPage = i; break; }
      }
      if (roughPage) break;
    }

    // 2차: 대략 위치 주변 세밀 검색
    if (roughPage && step > 1) {
      const from = Math.max(1, roughPage - step);
      const to = Math.min(pdfDoc.numPages, roughPage + step);
      for (let i = from; i <= to; i++) {
        if (i === roughPage) continue;
        const pg = await pdfDoc.getPage(i);
        const tc = await pg.getTextContent();
        let text = '';
        for (const it of tc.items) { if (it.str) text += it.str; }
        const norm = _hn(text);
        for (const anchor of anchors) {
          if (norm.includes(anchor)) return i < roughPage ? i : roughPage;
        }
      }
    }
    return roughPage;
  }

  // === PDF 페이지 라벨 → 물리적 페이지 매핑 ===
  async function _buildPageLabelMap(pdfDoc, fileId) {
    if (_pageLabelMap[fileId]) return _pageLabelMap[fileId];
    try {
      const labels = await pdfDoc.getPageLabels();
      if (labels && labels.length > 0) {
        const map = {};
        for (let i = 0; i < labels.length; i++) {
          map[labels[i]] = i + 1;  // 1-indexed 물리적 페이지
        }
        _pageLabelMap[fileId] = map;
        console.log('[JBTP] Page labels loaded:', Object.keys(map).length, 'entries');
        return map;
      }
    } catch(e) {
      console.warn('[JBTP] getPageLabels failed:', e);
    }
    // 폴백: 페이지 하단 텍스트에서 "- N -" 패턴 감지하여 오프셋 계산
    try {
      const map = {};
      // 샘플 페이지 5개 스캔하여 오프셋 결정
      const samplePages = [10, 20, 50, 100, Math.min(200, pdfDoc.numPages)];
      let offsets = [];
      for (const pn of samplePages) {
        if (pn > pdfDoc.numPages) continue;
        const pg = await pdfDoc.getPage(pn);
        const tc = await pg.getTextContent();
        let text = '';
        for (const it of tc.items) { if (it.str) text += it.str; }
        // "- 123 -" 패턴 매칭 (페이지 하단 번호)
        const m = text.match(/-\s*(\d+)\s*-\s*$/);
        if (m) {
          const printedPage = parseInt(m[1]);
          const offset = pn - printedPage;
          offsets.push(offset);
        }
      }
      if (offsets.length >= 2) {
        // 가장 빈번한 오프셋 사용
        const freq = {};
        offsets.forEach(o => { freq[o] = (freq[o] || 0) + 1; });
        const bestOffset = Number(Object.entries(freq).sort((a, b) => b[1] - a[1])[0][0]);
        // 오프셋으로 전체 매핑 생성
        for (let i = 1; i <= pdfDoc.numPages; i++) {
          const label = String(i - bestOffset);
          if (i - bestOffset > 0) map[label] = i;
        }
        _pageLabelMap[fileId] = map;
        console.log('[JBTP] Page offset detected:', bestOffset, '(sampled', offsets.length, 'pages)');
        return map;
      }
    } catch(e) {
      console.warn('[JBTP] Page offset detection failed:', e);
    }
    _pageLabelMap[fileId] = {};
    return {};
  }

  function _resolvePhysicalPage(fileId, pageLabel) {
    const map = _pageLabelMap[fileId];
    if (!map) return null;
    const key = String(pageLabel);
    return map[key] || null;
  }

  // === PDF Viewer (pdf.js canvas rendering) ===
  async function loadPdfPage(fileId, page, pageLabel, docContent) {
    const viewer = document.getElementById('jbtp-viewer');
    if (!viewer) return;
    const rawPageNum = page != null ? Number(page) + 1 : (pageLabel ? Number(pageLabel) : 1);

    if (!fileId) {
      if (docContent) {
        viewer.innerHTML =
          '<div class="jbtp-viewer-header"><span>p.' + (pageLabel || '?') + ' (텍스트)</span></div>' +
          '<div class="jbtp-text-content">' + escHtml(docContent) + '</div>';
      }
      return;
    }

    viewer.innerHTML =
      '<div class="jbtp-viewer-header"><span>p.' + rawPageNum + ' 로딩중...</span></div>' +
      '<div class="jbtp-viewer-placeholder">PDF를 불러오는 중...</div>';

    try {
      await ensurePdfJs();

      let pdfDoc = _pdfDocCache[fileId];
      if (!pdfDoc) {
        let blobUrl = _pdfBlobCache[fileId];
        if (!blobUrl) {
          const token = getToken();
          const fetchFn = _realFetch || fetch;
          const r = await fetchFn('/api/v1/files/' + fileId + '/content', {
            headers: { 'Authorization': 'Bearer ' + token }
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const blob = await r.blob();
          blobUrl = URL.createObjectURL(blob);
          _pdfBlobCache[fileId] = blobUrl;
        }
        console.log('[JBTP] Loading PDF with pdf.js, blobUrl:', blobUrl?.substring(0, 30));
        pdfDoc = await window.pdfjsLib.getDocument(blobUrl).promise;
        console.log('[JBTP] PDF loaded, pages:', pdfDoc.numPages);
        _pdfDocCache[fileId] = pdfDoc;
      }

      // 페이지 라벨 → 물리적 페이지 매핑
      await _buildPageLabelMap(pdfDoc, fileId);
      const displayLabel = pageLabel || rawPageNum;
      const physicalPage = _resolvePhysicalPage(fileId, displayLabel) || rawPageNum;
      if (physicalPage !== rawPageNum) {
        console.log('[JBTP] Page label resolved:', displayLabel, '→ physical:', physicalPage);
      }

      _renderPdfCanvas(viewer, pdfDoc, physicalPage, fileId);
      // 추출 텍스트 패널: 항상 표시 (LLM 답변 검증용)
      if (docContent) {
        _appendDocTextPanel(viewer, docContent, pageLabel || rawPageNum);
      }
    } catch(e) {
      console.error('[JBTP] PDF load error:', e);
      // fallback: 텍스트만 표시
      if (docContent) {
        viewer.innerHTML =
          '<div class="jbtp-viewer-header"><span>p.' + (pageLabel || rawPageNum) + ' (PDF 로딩 실패)</span>' +
          '<a href="/api/v1/files/' + fileId + '/content" target="_blank" class="jbtp-open-link">↗</a></div>' +
          '<div class="jbtp-doc-text-panel"><div class="jbtp-doc-text-header">\u{1F4DD} 추출 텍스트 (RAG 원본)</div>' +
          '<div class="jbtp-doc-text-body">' + escHtml(docContent) + '</div></div>';
      } else {
        const directUrl = '/api/v1/files/' + fileId + '/content#page=' + rawPageNum;
        viewer.innerHTML =
          '<div class="jbtp-viewer-header"><span>p.' + rawPageNum + '</span>' +
          '<a href="' + directUrl + '" target="_blank" class="jbtp-open-link">↗</a></div>' +
          '<iframe src="' + directUrl + '" class="jbtp-pdf-iframe"></iframe>';
      }
    }
  }

  // 추출 텍스트 패널 (PDF 아래에 항상 표시)
  function _appendDocTextPanel(viewer, docContent, pageLabel) {
    const existing = viewer.querySelector('.jbtp-doc-text-panel');
    if (existing) existing.remove();
    const panel = document.createElement('div');
    panel.className = 'jbtp-doc-text-panel';
    panel.innerHTML =
      '<div class="jbtp-doc-text-header">' +
        '<span>\u{1F4DD} RAG 추출 텍스트 (p.' + pageLabel + ') — LLM이 이 내용을 참고하여 답변했습니다</span>' +
        '<button class="jbtp-doc-text-collapse" onclick="this.parentElement.nextElementSibling.style.display=this.parentElement.nextElementSibling.style.display===\'none\'?\'block\':\'none\';this.textContent=this.textContent===\'\u25BC\'?\'\u25B2\':\'\u25BC\'">\u25B2</button>' +
      '</div>' +
      '<div class="jbtp-doc-text-body">' + escHtml(docContent) + '</div>';
    viewer.appendChild(panel);
  }

  function _renderPdfCanvas(viewer, pdfDoc, pageNum, fileId) {
    const safeNum = Math.max(1, Math.min(pageNum, pdfDoc.numPages));
    viewer.innerHTML =
      '<div class="jbtp-viewer-header">' +
        '<span>p.' + safeNum + ' / ' + pdfDoc.numPages + '</span>' +
        '<div class="jbtp-viewer-actions">' +
          '<button class="jbtp-nav-btn" data-dir="prev" title="이전 페이지">◀</button>' +
          '<button class="jbtp-nav-btn" data-dir="next" title="다음 페이지">▶</button>' +
          '<button class="jbtp-open-link" title="새 탭에서 열기">↗</button>' +
        '</div>' +
      '</div>' +
      '<div class="jbtp-canvas-wrap"><canvas class="jbtp-pdf-canvas"></canvas></div>';

    const wrap = viewer.querySelector('.jbtp-canvas-wrap');
    const canvas = viewer.querySelector('.jbtp-pdf-canvas');

    pdfDoc.getPage(safeNum).then(async pg => {
      const wrapW = wrap.clientWidth || (PANEL_WIDTH - 4);
      const vp1 = pg.getViewport({ scale: 1 });
      const scale = (wrapW / vp1.width) * 2;  // 2x for HiDPI
      const viewport = pg.getViewport({ scale: scale });
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      canvas.style.width = wrapW + 'px';
      canvas.style.height = (vp1.height * (wrapW / vp1.width)) + 'px';
      const ctx2d = canvas.getContext('2d');
      await pg.render({ canvasContext: ctx2d, viewport: viewport }).promise;

      // PDF 페이지 내 텍스트 하이라이팅 (환각 검증용)
      if (_highlightDocText) {
        try {
          // 정규화: 공백 + NULL(\u0000) + NBSP 제거
          const _hn = t => t.replace(/[\s\u0000\u00a0]+/g, '');

          const tc = await pg.getTextContent();
          let fullText = '';
          const bounds = [];
          for (const it of tc.items) {
            if (!it.str) continue;
            const start = fullText.length;
            fullText += it.str;
            bounds.push({ start, end: fullText.length, item: it });
          }
          const hlNorm = _hn(_highlightDocText);
          const fullNorm = _hn(fullText);

          // 정규화 인덱스 → 원본 인덱스 매핑
          const n2o = [];
          for (let i = 0; i < fullText.length; i++) {
            if (_hn(fullText[i]).length > 0) n2o.push(i);
          }

          // 여러 앵커 위치 시도 (시작, 1/4, 1/2, 3/4)
          let pos = -1;
          const anchorLen = Math.min(30, hlNorm.length);
          if (anchorLen >= 8) {
            for (const r of [0, 0.25, 0.5, 0.75]) {
              const off = Math.floor(r * Math.max(0, hlNorm.length - anchorLen));
              const anchor = hlNorm.slice(off, off + anchorLen);
              const p = fullNorm.indexOf(anchor);
              if (p >= 0) { pos = Math.max(0, p - off); break; }
            }
          }

          if (pos >= 0) {
            const oStart = n2o[pos] || 0;
            const endIdx = Math.min(pos + hlNorm.length - 1, n2o.length - 1);
            const oEnd = n2o[endIdx] || fullText.length;
            let firstY = null;
            ctx2d.fillStyle = 'rgba(255, 220, 0, 0.35)';
            for (const b of bounds) {
              if (b.end <= oStart || b.start >= oEnd) continue;
              const it = b.item;
              const tx = it.transform;
              const fs = Math.sqrt(tx[0]*tx[0] + tx[1]*tx[1]);
              const x = tx[4], y = tx[5];
              const p1 = viewport.convertToViewportPoint(x, y - fs * 0.2);
              const p2 = viewport.convertToViewportPoint(x + (it.width || fs * it.str.length * 0.5), y + fs);
              const rx = Math.min(p1[0], p2[0]);
              const ry = Math.min(p1[1], p2[1]);
              ctx2d.fillRect(rx, ry, Math.abs(p2[0]-p1[0]), Math.abs(p2[1]-p1[1]));
              if (firstY === null) firstY = ry;
            }
            if (firstY !== null) {
              const displayH = parseFloat(canvas.style.height);
              const canvasH = canvas.height;
              const scrollY = (firstY / canvasH) * displayH - 40;
              if (scrollY > 50) wrap.scrollTop = scrollY;
              console.log('[JBTP] Highlighted on page', safeNum, 'scrollY:', Math.round(scrollY));
            }
            _highlightDocText = '';
            _highlightRetried = false;
          } else if (!_highlightRetried) {
            // 현재 페이지에서 못 찾음 → 인접 페이지 ±1,±2 검색
            _highlightRetried = true;
            let foundPage = 0;
            for (const delta of [1, -1, 2, -2, 3, -3, 5, -5, 8, -8, 10, -10]) {
              const tryNum = safeNum + delta;
              if (tryNum < 1 || tryNum > pdfDoc.numPages) continue;
              const tryPg = await pdfDoc.getPage(tryNum);
              const tryTc = await tryPg.getTextContent();
              let tryText = '';
              for (const it of tryTc.items) { if (it.str) tryText += it.str; }
              const tryNorm = _hn(tryText);
              for (const r of [0, 0.25, 0.5, 0.75]) {
                const off = Math.floor(r * Math.max(0, hlNorm.length - anchorLen));
                const anchor = hlNorm.slice(off, off + anchorLen);
                if (anchor.length >= 8 && tryNorm.includes(anchor)) {
                  foundPage = tryNum;
                  break;
                }
              }
              if (foundPage) break;
            }
            if (foundPage > 0) {
              console.log('[JBTP] Text on nearby page', foundPage, '(expected', safeNum, ')');
              const hdr = viewer.querySelector('.jbtp-viewer-header span');
              if (hdr) hdr.textContent = 'p.' + foundPage + ' / ' + pdfDoc.numPages;
              _renderPdfCanvas(viewer, pdfDoc, foundPage, fileId);
              return;
            }
            console.log('[JBTP] Text not found on nearby pages');
            _highlightDocText = '';
            _highlightRetried = false;
          } else {
            _highlightDocText = '';
            _highlightRetried = false;
          }
        } catch(e) {
          console.warn('[JBTP] Highlight error:', e);
          _highlightDocText = '';
          _highlightRetried = false;
        }
      }
    });

    // 네비게이션
    let cur = safeNum;
    viewer.querySelector('.jbtp-open-link').onclick = () => {
      const u = _pdfBlobCache[fileId];
      if (u) window.open(u + '#page=' + cur, '_blank');
    };
    viewer.querySelectorAll('.jbtp-nav-btn').forEach(btn => {
      btn.onclick = () => {
        const np = btn.dataset.dir === 'prev' ? Math.max(1, cur - 1) : Math.min(pdfDoc.numPages, cur + 1);
        if (np === cur) return;
        cur = np;
        _renderPdfCanvas(viewer, pdfDoc, cur, fileId);
      };
    });
  }

  // === 출처 버튼 클릭 인터셉트 ===
  function setupSourceClickIntercept() {
    document.body.addEventListener('click', async (e) => {
      // 패턴 1: "View source: filename" (단일 소스)
      const viewSrcBtn = e.target.closest('button[aria-label^="View source:"]');
      if (viewSrcBtn) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        const label = viewSrcBtn.getAttribute('aria-label') || '';
        const filename = label.replace('View source: ', '').trim();
        const contextText = _getContextText(viewSrcBtn);
        console.log('[JBTP] View source clicked:', filename, 'ctx:', contextText?.slice(-60));
        await openPanelForSource(filename, contextText);
        return;
      }

      // 패턴 2: "domain.co.kr +N more sources" (다중 소스) — v6에서 누락됐던 부분
      const btn = e.target.closest('button');
      if (btn) {
        const text = (btn.textContent || '').trim();

        // "+N more sources" 또는 "+N" 패턴
        const multiMatch = text.match(/^([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\s*\+\d/);
        if (multiMatch) {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          const domain = multiMatch[1];
          console.log('[JBTP] Multi-source clicked, domain:', domain);
          await openPanelForSource(domain);
          return;
        }

        // "N개의 소스" 버튼
        if (text.match(/\d+개의 소스/)) {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          console.log('[JBTP] Toggle all sources clicked');
          await openPanelForSource(null);
          return;
        }
      }
    }, true);
  }

  async function openPanelForSource(matchStr, contextText) {
    if (!panel) createPanel();

    const chatMatch = location.pathname.match(/\/c\/([0-9a-f-]{36})/);
    if (!chatMatch) return;
    const chatId = chatMatch[1];

    // 채팅이 바뀌었으면 캐시 강제 클리어
    if (chatId !== _lastChatId) {
      _cachedSources = [];
      lastRenderedKey = '';
      _lastChatId = chatId;
      console.log('[JBTP] Chat changed, clearing cache. New chat:', chatId);
    }

    // matchStr에서 도메인 추출 (domain.co.kr 형태면 그대로, 파일명이면 null)
    const isDomain = matchStr && /^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(matchStr);
    const matchDomain = isDomain ? matchStr.replace(/^www\./, '') : null;

    if (_cachedSources.length > 0) {
      renderSources(_cachedSources, matchDomain || matchStr, contextText);
      togglePanel(true);
      return;
    }

    // 캐시 없으면 API에서 가져오기
    togglePanel(true);

    try {
      const token = getToken();
      const fetchFn = _realFetch || fetch;
      const resp = await fetchFn('/api/v1/chats/' + chatId, {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      const data = await resp.json();
      extractSourcesFromChat(data);
      if (_cachedSources.length > 0) {
        renderSources(_cachedSources, matchDomain || matchStr, contextText);
      }
    } catch(e) {
      console.error('[JBTP] Chat fetch error:', e);
    }
  }

  // === URL 변경 감지 ===
  function setupUrlWatcher() {
    let lastUrl = location.href;
    setInterval(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        lastRenderedKey = '';
        _cachedSources = [];
        if (panelVisible) {
          togglePanel(false);
        }
      }
    }, 1000);
  }

  // === Utilities ===
  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  // === Panel DOM ===
  function createPanel() {
    if (document.getElementById('jbtp-citation-panel')) return;

    panel = document.createElement('div');
    panel.id = 'jbtp-citation-panel';
    panel.innerHTML =
      '<div class="jbtp-panel-header">' +
        '<span class="jbtp-panel-title">\u{1F4C4} \uC778\uC6A9 \uD398\uC774\uC9C0</span>' +
        '<div class="jbtp-panel-controls">' +
          '<button class="jbtp-btn-close" title="\uB2EB\uAE30">\u00D7 \uB044\uAE30</button>' +
        '</div>' +
      '</div>' +
      '<div class="jbtp-panel-sources" id="jbtp-source-list">' +
        '<div class="jbtp-empty-msg">\uCD9C\uCC98\uB97C \uD074\uB9AD\uD558\uBA74 \uC778\uC6A9 \uD398\uC774\uC9C0\uAC00 \uD45C\uC2DC\uB429\uB2C8\uB2E4.</div>' +
      '</div>' +
      '<div class="jbtp-panel-viewer" id="jbtp-viewer">' +
        '<div class="jbtp-viewer-placeholder">\uC18C\uC2A4\uB97C \uD074\uB9AD\uD558\uBA74 \uB0B4\uC6A9\uC774 \uD45C\uC2DC\uB429\uB2C8\uB2E4</div>' +
      '</div>';
    document.body.appendChild(panel);

    panel.querySelector('.jbtp-btn-close').onclick = () => togglePanel(false);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && panelVisible) togglePanel(false);
    });
  }

  function togglePanel(forceShow) {
    if (!panel) createPanel();
    panelVisible = forceShow !== undefined ? forceShow : !panelVisible;
    panel.style.display = panelVisible ? 'flex' : 'none';

    const main = document.querySelector('.relative.flex.flex-col.flex-auto');
    if (main) main.style.marginRight = panelVisible ? PANEL_WIDTH + 'px' : '0';
  }

  // === Styles ===
  const style = document.createElement('style');
  style.textContent = `
    #jbtp-citation-panel {
      position:fixed; right:0; top:0; width:${PANEL_WIDTH}px; height:100vh;
      background:#0d1117; color:#c9d1d9;
      font-family:-apple-system,"Segoe UI","Noto Sans KR",sans-serif; font-size:13px;
      display:none; flex-direction:column; z-index:9999;
      border-left:2px solid #30363d; box-shadow:-4px 0 16px rgba(0,0,0,0.4);
    }
    .jbtp-panel-header {
      display:flex; justify-content:space-between; align-items:center;
      padding:10px 16px; background:#161b22; border-bottom:1px solid #30363d; flex-shrink:0;
    }
    .jbtp-panel-title { font-weight:600; font-size:14px; color:#58a6ff; }
    .jbtp-panel-controls button {
      background:#da3633; border:none; color:#fff; cursor:pointer;
      font-size:13px; font-weight:600; padding:5px 12px; line-height:1;
      border-radius:6px; display:flex; align-items:center; gap:4px;
    }
    .jbtp-panel-controls button:hover { background:#f85149; }

    /* 소스 목록 */
    .jbtp-panel-sources {
      flex:0 0 auto; max-height:25%; overflow-y:auto;
      border-bottom:1px solid #30363d; padding:4px 0;
    }
    .jbtp-empty-msg { color:#484f58; text-align:center; padding:20px 16px; line-height:1.6; }

    /* 소스 아이템 공통 */
    .jbtp-source-item { border-bottom:1px solid #21262d; }
    .jbtp-source-item.jbtp-highlight { background:#1c2333; }

    /* 웹 소스 */
    .jbtp-web-source .jbtp-source-header {
      padding:8px 16px; display:flex; align-items:center; gap:10px;
      transition:background 0.15s;
    }
    .jbtp-web-source .jbtp-source-header:hover { background:#161b22; }
    .jbtp-favicon { width:16px; height:16px; flex-shrink:0; border-radius:2px; }
    .jbtp-web-info { display:flex; flex-direction:column; gap:2px; min-width:0; }
    .jbtp-web-title {
      color:#c9d1d9; font-weight:500; font-size:13px;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }
    .jbtp-web-url { color:#484f58; font-size:11px; }
    .jbtp-highlight .jbtp-web-title { color:#58a6ff; font-weight:600; }
    .jbtp-web-actions {
      display:flex; gap:8px; padding:4px 16px 8px 42px;
    }
    .jbtp-web-open {
      color:#58a6ff; font-size:12px; text-decoration:none;
      padding:3px 10px; border-radius:4px;
      background:#21262d; border:1px solid #30363d;
    }
    .jbtp-web-open:hover { background:#30363d; border-color:#58a6ff; }
    .jbtp-web-text-btn {
      color:#8b949e; font-size:12px; cursor:pointer;
      padding:3px 10px; border-radius:4px;
      background:#21262d; border:1px solid #30363d;
      font-family:inherit;
    }
    .jbtp-web-text-btn:hover { background:#30363d; color:#c9d1d9; }

    /* PDF 소스 */
    .jbtp-pdf-source .jbtp-source-header {
      padding:8px 16px; cursor:pointer; display:flex; align-items:flex-start;
      gap:6px; transition:background 0.15s;
    }
    .jbtp-pdf-source .jbtp-source-header:hover { background:#161b22; }
    .jbtp-source-idx { color:#f0883e; font-weight:700; flex-shrink:0; }
    .jbtp-source-name { color:#c9d1d9; word-break:break-all; line-height:1.4; font-size:12px; }
    .jbtp-highlight .jbtp-source-name { color:#58a6ff; font-weight:600; }
    .jbtp-page-list { display:none; flex-wrap:wrap; gap:4px; padding:4px 16px 8px 32px; }
    .jbtp-source-item.expanded .jbtp-page-list { display:flex; }
    .jbtp-page-btn {
      background:#21262d; border:1px solid #30363d; color:#58a6ff;
      padding:3px 10px; border-radius:4px; cursor:pointer;
      font-family:inherit; font-size:12px; transition:all 0.15s;
    }
    .jbtp-page-btn:hover { background:#30363d; border-color:#58a6ff; }
    .jbtp-page-btn.active { background:#1f6feb; color:#fff; border-color:#58a6ff; }

    /* 뷰어 영역 */
    .jbtp-panel-viewer { flex:1; display:flex; flex-direction:column; min-height:0; }
    .jbtp-viewer-placeholder { color:#484f58; text-align:center; padding:40px 16px; }
    .jbtp-viewer-header {
      display:flex; justify-content:space-between; align-items:center;
      padding:8px 16px; background:#161b22; border-bottom:1px solid #30363d;
      color:#c9d1d9; font-size:13px; flex-shrink:0; font-weight:500;
    }
    .jbtp-viewer-actions { display:flex; gap:6px; align-items:center; }
    .jbtp-nav-btn {
      background:#21262d; border:1px solid #30363d; color:#8b949e;
      padding:2px 8px; border-radius:3px; cursor:pointer; font-size:12px;
    }
    .jbtp-nav-btn:hover { background:#30363d; color:#c9d1d9; }
    .jbtp-open-link {
      color:#58a6ff; text-decoration:none; font-size:16px;
      background:none; border:none; cursor:pointer; padding:0 4px;
    }
    .jbtp-open-link:hover { color:#79c0ff; }
    .jbtp-pdf-iframe { flex:1; width:100%; border:none; background:#fff; }
    .jbtp-canvas-wrap { flex:1; overflow-y:auto; background:#525659; padding:8px 0; display:flex; justify-content:center; align-items:flex-start; }
    .jbtp-pdf-canvas { display:block; box-shadow:0 2px 8px rgba(0,0,0,0.3); }
    .jbtp-text-content {
      flex:1; overflow-y:auto; padding:14px 16px;
      white-space:pre-wrap; line-height:1.7; color:#adbac7; font-size:13px;
    }

    /* 추출 텍스트 패널 */
    .jbtp-doc-text-panel {
      border-top:2px solid #f0883e;
      background:#161b22;
      flex-shrink:0;
      max-height:250px;
      display:flex;
      flex-direction:column;
    }
    .jbtp-doc-text-header {
      display:flex; justify-content:space-between; align-items:center;
      padding:8px 12px; background:#1c2128; font-size:12px; color:#f0883e;
      font-weight:600; flex-shrink:0; border-bottom:1px solid #30363d;
    }
    .jbtp-doc-text-collapse {
      background:none; border:none; color:#8b949e; cursor:pointer; font-size:14px; padding:2px 6px;
    }
    .jbtp-doc-text-body {
      overflow-y:auto; padding:10px 14px; font-size:13px;
      line-height:1.8; color:#c9d1d9; white-space:pre-wrap; word-break:break-word;
    }
    .jbtp-doc-text-body::-webkit-scrollbar { width:6px; }
    .jbtp-doc-text-body::-webkit-scrollbar-track { background:#161b22; }
    .jbtp-doc-text-body::-webkit-scrollbar-thumb { background:#30363d; border-radius:3px; }

    /* 웹 뷰어 */
    .jbtp-web-viewer {
      flex:1; overflow-y:auto; padding:0;
    }
    .jbtp-web-meta {
      padding:12px 16px; display:flex; align-items:center; gap:10px;
      border-bottom:1px solid #21262d; flex-wrap:wrap;
    }
    .jbtp-web-domain-badge {
      background:#1f6feb; color:#fff; padding:2px 10px; border-radius:12px;
      font-size:11px; font-weight:500;
    }
    .jbtp-web-fullurl {
      color:#484f58; font-size:11px; text-decoration:none;
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%;
    }
    .jbtp-web-fullurl:hover { color:#58a6ff; }
    .jbtp-web-desc {
      padding:10px 16px; color:#8b949e; font-size:12px; line-height:1.5;
      border-bottom:1px solid #21262d; font-style:italic;
    }
    .jbtp-web-body {
      padding:16px; color:#c9d1d9; font-size:14px; line-height:1.8;
      white-space:pre-wrap; word-break:break-word;
    }

    /* 스크롤바 */
    .jbtp-panel-sources::-webkit-scrollbar,
    .jbtp-web-viewer::-webkit-scrollbar,
    .jbtp-text-content::-webkit-scrollbar { width:6px; }
    .jbtp-panel-sources::-webkit-scrollbar-track,
    .jbtp-web-viewer::-webkit-scrollbar-track,
    .jbtp-text-content::-webkit-scrollbar-track { background:#0d1117; }
    .jbtp-panel-sources::-webkit-scrollbar-thumb,
    .jbtp-web-viewer::-webkit-scrollbar-thumb,
    .jbtp-text-content::-webkit-scrollbar-thumb { background:#30363d; border-radius:3px; }
  `;
  document.head.appendChild(style);

  // === Init ===
  function init() {
    createPanel();
    installFetchIntercept();
    setupSourceClickIntercept();
    setupUrlWatcher();
    console.log('[JBTP] Citation Panel v7 loaded');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/*status_widget_start*/
(function(){
  'use strict';
  var POLL_MS = 15000;
  var API = '/monitoring/api/public-status';
  var _open = false;
  var _data = null;

  function c(tag,cls,html){var e=document.createElement(tag);if(cls)e.className=cls;if(html)e.innerHTML=html;return e;}

  function pctColor(v){return v>=90?'#e74c3c':v>=70?'#f39c12':'#2ecc71';}

  function buildSparkline(history){
    if(!history||history.length<2)return '<div style="margin-top:8px;padding:10px;background:#252540;border-radius:8px;border:1px solid #333;text-align:center;color:#666;font-size:11px">그래프 데이터 수집 중...</div>';
    var W=288,H=80,pad=2;
    var vals=history.map(function(h){return h.pct});
    var maxV=Math.max(100,Math.max.apply(null,vals));
    var step=(W-pad*2)/(vals.length-1);
    var pts=vals.map(function(v,i){return (pad+i*step).toFixed(1)+','+(H-pad-(v/maxV)*(H-pad*2-10)).toFixed(1)});
    var polyline=pts.join(' ');
    var fillPts=pts.join(' ')+' '+(pad+(vals.length-1)*step).toFixed(1)+','+H+' '+pad+','+H;
    var lastPt=pts[pts.length-1].split(',');
    var t0=history[0],tN=history[history.length-1];
    var d0=new Date(t0.ts*1000),dN=new Date(tN.ts*1000);
    var fmt=function(dt){return dt.getHours().toString().padStart(2,'0')+':'+dt.getMinutes().toString().padStart(2,'0')};
    return '<div style="margin-top:8px;padding:8px 10px;background:#252540;border-radius:8px;border:1px solid #333">'+
      '<div style="display:flex;justify-content:space-between;margin-bottom:4px">'+
        '<span style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px">워커 사용률 추이</span>'+
        '<span style="font-size:10px;color:#666;font-family:monospace">'+fmt(d0)+' ~ '+fmt(dN)+'</span>'+
      '</div>'+
      '<svg width="100%" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" style="display:block">'+
        '<defs><linearGradient id="swg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#e74c3c" stop-opacity="0.3"/><stop offset="100%" stop-color="#e74c3c" stop-opacity="0.02"/></linearGradient></defs>'+
        '<line x1="'+pad+'" y1="'+(H-pad-(90/maxV)*(H-pad*2-10)).toFixed(1)+'" x2="'+(W-pad)+'" y2="'+(H-pad-(90/maxV)*(H-pad*2-10)).toFixed(1)+'" stroke="#e74c3c" stroke-width="0.5" stroke-dasharray="3,3" opacity="0.5"/>'+
        '<text x="'+(W-pad-2)+'" y="'+(H-pad-(90/maxV)*(H-pad*2-10)-2).toFixed(1)+'" fill="#e74c3c" font-size="7" text-anchor="end" opacity="0.7">90%</text>'+
        '<polygon points="'+fillPts+'" fill="url(#swg)"/>'+
        '<polyline points="'+polyline+'" fill="none" stroke="#e74c3c" stroke-width="1.5" stroke-linejoin="round"/>'+
        '<circle cx="'+lastPt[0]+'" cy="'+lastPt[1]+'" r="3" fill="#e74c3c" stroke="#252540" stroke-width="1.5"/>'+
      '</svg>'+
    '</div>';
  }

  function statusDot(s){
    var color = s==='healthy'||s==='running'||s==='ok'?'#2ecc71':s==='unknown'?'#95a5a6':'#e74c3c';
    return '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+color+';margin-right:4px"></span>';
  }

  function fmtNum(n){return n==null?'--':n.toLocaleString();}

  function renderPanel(){
    var p=document.getElementById('owi-status-panel');
    if(!p||!_data)return;
    var d=_data;
    var wPct=d.worker_usage_pct||0;
    p.innerHTML=
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+
        '<div class="sw-card">'+
          '<div class="sw-label">워커 사용률</div>'+
          '<div class="sw-val" style="color:'+pctColor(wPct)+'">'+wPct+'%</div>'+
          '<div class="sw-sub">'+fmtNum(d.worker_conns)+' / '+fmtNum(d.worker_total)+'</div>'+
        '</div>'+
        '<div class="sw-card">'+
          '<div class="sw-label">동시접속</div>'+
          '<div class="sw-val" style="color:#3498db">'+fmtNum(d.active_users)+'</div>'+
          '<div class="sw-sub">최근 5분 활성</div>'+
        '</div>'+
        '<div class="sw-card">'+
          '<div class="sw-label">Nginx 연결</div>'+
          '<div class="sw-val" style="color:#e67e22">'+fmtNum(d.nginx_active)+'</div>'+
          '<div class="sw-sub">active connections</div>'+
        '</div>'+
        '<div class="sw-card">'+
          '<div class="sw-label">PostgreSQL</div>'+
          '<div class="sw-val" style="color:#2c3e50">'+fmtNum(d.pg_connections)+'</div>'+
          '<div class="sw-sub">/ '+fmtNum(d.pg_max)+' max</div>'+
        '</div>'+
        '<div class="sw-card">'+
          '<div class="sw-label">Qdrant</div>'+
          '<div class="sw-val" style="color:#8e44ad">'+fmtNum(d.qdrant_collections)+'</div>'+
          '<div class="sw-sub">'+fmtNum(d.qdrant_points)+' vectors</div>'+
        '</div>'+
        '<div class="sw-card">'+
          '<div class="sw-label">Redis</div>'+
          '<div class="sw-val" style="color:#c0392b">'+fmtNum(d.redis_clients)+'</div>'+
          '<div class="sw-sub">clients</div>'+
        '</div>'+
        '<div class="sw-card" style="grid-column:span 2">'+
          '<div class="sw-label">서비스 상태</div>'+
          '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:4px;font-size:12px">'+
            statusDot(d.graphrag&&d.graphrag.status)+'<span>날리지그래프 ('+fmtNum(d.graphrag&&d.graphrag.nodes)+' nodes)</span>'+
            '<span style="margin-left:8px">'+statusDot(d.semantica&&d.semantica.status)+'Semantica</span>'+
            '<span style="margin-left:8px">'+statusDot(d.voice)+'음성대화</span>'+
          '</div>'+
        '</div>'+
      '</div>'+
      '<div style="margin-top:8px">'+
        '<div style="height:8px;background:#333;border-radius:4px;overflow:hidden">'+
          '<div style="height:100%;width:'+Math.min(wPct,100)+'%;background:'+pctColor(wPct)+';border-radius:4px;transition:width 0.5s"></div>'+
        '</div>'+
        '<div style="display:flex;justify-content:space-between;margin-top:3px;font-size:10px;color:#666;font-family:monospace">'+
          '<span>active: '+fmtNum(d.nginx_active)+'</span>'+
          '<span>waiting: '+(d.worker_waiting||0)+'</span>'+
        '</div>'+
      '</div>'+
      buildSparkline(d.worker_history_1h||[]);
  }

  function fetchStatus(){
    fetch(API).then(function(r){return r.json()}).then(function(d){
      _data=d;
      var btn=document.getElementById('owi-status-btn');
      if(btn){
        var pct=d.worker_usage_pct||0;
        btn.style.background=pct>=90?'#e74c3c':pct>=70?'#f39c12':'#2ecc71';
        btn.title='워커 '+pct+'% | 동시접속 '+(d.active_users||0)+'명';
      }
      if(_open)renderPanel();
    }).catch(function(){});
  }

  function initWidget(){
    var style=c('style');
    style.textContent=
      '#owi-status-btn{position:fixed;bottom:80px;left:16px;z-index:9999;width:14px;height:14px;border-radius:50%;background:#2ecc71;border:2px solid rgba(255,255,255,0.3);cursor:pointer;transition:all 0.3s;box-shadow:0 0 8px rgba(46,204,113,0.4)}'+
      '#owi-status-btn:hover{transform:scale(1.5);box-shadow:0 0 14px rgba(46,204,113,0.6)}'+
      '#owi-status-panel-wrap{position:fixed;bottom:100px;left:16px;z-index:9998;width:320px;background:#1a1a2e;border:1px solid #333;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.5);padding:14px;display:none;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:13px;max-height:70vh;overflow-y:auto}'+
      '.sw-card{background:#252540;border-radius:8px;padding:8px 10px;border:1px solid #333}'+
      '.sw-label{font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px}'+
      '.sw-val{font-size:20px;font-weight:800;font-family:"JetBrains Mono",monospace;line-height:1.2}'+
      '.sw-sub{font-size:10px;color:#666;font-family:monospace}';
    document.head.appendChild(style);

    var btn=c('div');btn.id='owi-status-btn';btn.title='시스템 상태';
    btn.onclick=function(){
      _open=!_open;
      var pw=document.getElementById('owi-status-panel-wrap');
      if(pw)pw.style.display=_open?'block':'none';
      if(_open)fetchStatus();
    };
    document.body.appendChild(btn);

    var pw=c('div');pw.id='owi-status-panel-wrap';
    pw.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><span style="font-weight:700;font-size:14px">System Status</span><span id="owi-status-close" style="cursor:pointer;color:#666;font-size:18px">&times;</span></div><div id="owi-status-panel"></div>';
    document.body.appendChild(pw);
    pw.querySelector('#owi-status-close').onclick=function(){_open=false;pw.style.display='none';};

    fetchStatus();
    setInterval(fetchStatus,POLL_MS);
  }

  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',initWidget);}
  else{initWidget();}
})();
/*status_widget_end*/
