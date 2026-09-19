// content.js - Runs on Testbook, PW/DPPS, and Oliveboard pages. Injects a floating button.
// Strategy: Scrape aggressively with multiple selector fallbacks per platform.
// The raw_html field lets the server-side model extract structure even when selectors miss.

(function () {
  'use strict';

  const SERVER_URL = 'http://localhost:8787';

  // Detect which platform we're on
  function getSitePlatform() {
    const host = window.location.hostname;
    if (host.includes('pw.live')) return 'pw';
    if (host.includes('oliveboard.in')) return 'oliveboard';
    if (host.includes('testranking.in')) return 'testranking';
    return 'testbook';
  }

  const PLATFORM = getSitePlatform();

  // Per-platform question card selectors (tried in order, first match wins)
  const PLATFORM_SELECTORS = {
    testbook: [
      // Old Testbook Angular UI (solutions/review pages: #/lt-solutions)
      '.qpip-inner.active .que-ans-box',  // active question on single-question review
      '#questions .que-ans-box',           // same, id-based fallback
      '.que-ans-box',                      // any question card
      '.body.detailed-question',           // question body wrapper
      // New Testbook React UI
      '.question-card',
      '.question-container',
      '[class*="question-item"]',
      '[class*="QuestionCard"]',
      '[class*="questionCard"]',
      '[class*="QuesCard"]',
      '[class*="QuesContainer"]',
      '[class*="questionWrapper"]',
      '[class*="QuestionWrapper"]',
      '.analysis-question',
      '.sol-question',
    ],
    pw: [
      '[class*="QuestionCard"]',
      '[class*="question-card"]',
      '[class*="questionCard"]',
      '[class*="QuizQuestion"]',
      '[class*="result-question"]',
      '[class*="question-wrapper"]',
      '[class*="questionWrapper"]',
    ],
    oliveboard: [
      // Oliveboard "Solution App" (exams/solution/index3.php): each question is a
      // .qosblock.singleqid wrapper; only the currently navigated one is display:flex,
      // the rest are display:none with un-decoded (encoded) content. Real scraping goes
      // through the dedicated Oliveboard path (collectOliveboard) which drives goToQuestion.
      '.qosblock.singleqid',
      '.singlequestion',
      // Legacy/other Oliveboard layouts (kept as fallbacks)
      '[class*="question-container"]',
      '[class*="questionContainer"]',
      '[class*="quiz-question"]',
    ],
    // TestRanking capture is API-based (collectTestranking) — no DOM selectors.
    testranking: [],
  };

  // All server calls go through the service worker to bypass Chrome's
  // mixed-content block (HTTPS page -> HTTP localhost fails from content
  // scripts, but works from the extension background context).
  const DISCONNECTED_MSG = 'Extension reloaded — refresh this page (F5) and try again';

  function serverFetch(path, options = {}) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage(
          {
            type: 'fetch',
            path,
            options: {
              method: options.method || 'GET',
              headers: options.headers || {},
              body: options.body,
            },
          },
          (response) => {
            if (chrome.runtime.lastError) {
              const msg = chrome.runtime.lastError.message || '';
              if (/context invalidated|could not establish connection|receiving end does not exist/i.test(msg)) {
                reject(new Error(DISCONNECTED_MSG));
              } else {
                reject(new Error(msg));
              }
              return;
            }
            if (!response) {
              reject(new Error('No response from background script'));
              return;
            }
            if (!response.ok && response.error) {
              reject(new Error(response.error));
              return;
            }
            try {
              resolve(JSON.parse(response.body));
            } catch (e) {
              reject(new Error('Invalid JSON from server: ' + response.body.slice(0, 100)));
            }
          },
        );
      } catch (e) {
        // sendMessage itself throws when the content script context is invalidated
        reject(new Error(DISCONNECTED_MSG));
      }
    });
  }

  // ---------- Inject floating action button ----------
  function injectButton() {
    if (document.getElementById('tb-error-logger-btn')) return;

    const btn = document.createElement('div');
    btn.id = 'tb-error-logger-btn';
    btn.innerHTML = `
      <div class="tb-el-main" title="Log mistakes on this page">
        <span class="tb-el-icon">📝</span>
        <span class="tb-el-label">Log Mistakes</span>
        <span class="tb-el-count" id="tb-el-count"></span>
      </div>
      <div class="tb-el-menu" id="tb-el-menu">
        <div class="tb-el-menu-item" data-action="scan">🔍 Scan page for wrong answers</div>
        <div class="tb-el-menu-item" data-action="current">📌 Log current question only</div>
        <div class="tb-el-menu-item" data-action="open">📊 Open dashboard</div>
      </div>
    `;
    document.body.appendChild(btn);

    const menu = btn.querySelector('#tb-el-menu');
    btn.querySelector('.tb-el-main').addEventListener('click', (e) => {
      e.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', () => menu.classList.remove('open'));

    btn.querySelectorAll('.tb-el-menu-item').forEach((item) => {
      item.addEventListener('click', async (e) => {
        e.stopPropagation();
        menu.classList.remove('open');
        const action = item.dataset.action;
        if (action === 'scan') await handleScan();
        else if (action === 'current') await handleCurrent();
        else if (action === 'open') window.open(SERVER_URL + '/', '_blank');
      });
    });
  }

  // ---------- Toast notifications ----------
  function toast(msg, type = 'info') {
    const t = document.createElement('div');
    t.className = `tb-el-toast tb-el-toast-${type}`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.classList.add('show'), 10);
    setTimeout(() => {
      t.classList.remove('show');
      setTimeout(() => t.remove(), 300);
    }, 3500);
  }

  // ---------- Scraping helpers ----------

  // Clone an element and strip our own injected UI before reading text.
  // innerText on a detached node doesn't work (needs layout), so we
  // temporarily hide the real button instead, read innerText, then restore.
  //
  // KaTeX/MathJax renders each math character as an absolutely-positioned span,
  // so innerText reads them in DOM order (one char per line, scrambled). We fix
  // this by temporarily replacing each outermost math node with a plain-text span
  // containing its original LaTeX (from the hidden MathML annotation element).
  // Serialize a MathML <math> subtree to readable linear text, preserving the
  // structural separators that plain textContent throws away. The classic bug:
  // <mfrac><mn>78</mn><mn>255</mn></mfrac> has textContent "78255" — numerator
  // and denominator run together, so "15 78/255" collapses to "1578255". We walk
  // the tree and emit "/" for fractions, "^" for exponents, etc.
  function mathmlToText(node) {
    if (!node) return '';
    if (node.nodeType === 3) return node.nodeValue.replace(/\s+/g, ''); // text node: drop layout whitespace
    if (node.nodeType !== 1) return '';
    const tag = node.tagName.toLowerCase().replace(/^[a-z]+:/, ''); // strip ns prefix (m:mfrac)
    const kids = Array.from(node.childNodes);
    const seq = (sep) => kids.map(mathmlToText).filter((s) => s !== '').join(sep);
    const wrap = (s) => (/[\s+\-*/^()]/.test(s) ? `(${s})` : s);
    switch (tag) {
      case 'mfrac':
        return `${wrap(mathmlToText(node.children[0]))}/${wrap(mathmlToText(node.children[1]))}`;
      case 'msup':
        return `${mathmlToText(node.children[0])}^${wrap(mathmlToText(node.children[1]))}`;
      case 'msub':
        return `${mathmlToText(node.children[0])}_${wrap(mathmlToText(node.children[1]))}`;
      case 'msubsup':
        return `${mathmlToText(node.children[0])}_${wrap(mathmlToText(node.children[1]))}^${wrap(mathmlToText(node.children[2]))}`;
      case 'msqrt':
        return `√(${seq('')})`;
      case 'mroot':
        return `(${mathmlToText(node.children[0])})^(1/${mathmlToText(node.children[1])})`;
      case 'mn':
      case 'mi':
      case 'mo':
      case 'mtext':
        return (node.textContent || '').trim();
      case 'mspace':
        return ' ';
      case 'annotation':
        return ''; // skip the LaTeX-source duplicate carried alongside presentation MathML
      default:
        // mrow / math / semantics / mstyle / mpadded / unknown: join children with a
        // space so adjacent atoms ("15" then a fraction) don't fuse into one number.
        return seq(' ').replace(/\s+/g, ' ').trim();
    }
  }

  // ---------- Image helpers ----------
  // Question figures, image-only options, and solution diagrams are all plain <img>
  // tags (Testbook serves them from storage.googleapis.com/tb-img). innerText drops
  // them entirely, so image-based questions used to be captured as bare text.

  function isContentImage(img) {
    const src = img.currentSrc || img.src || '';
    if (!src || src.startsWith('data:') || src.startsWith('chrome-extension:')) return false;
    if (img.closest('#tb-error-logger-btn')) return false;
    // Skip tiny UI icons/spinners. If the image hasn't loaded yet (0×0), keep it.
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    if (w && h && (w < 24 || h < 24)) return false;
    return true;
  }

  function imageName(src) {
    try {
      return decodeURIComponent(new URL(src).pathname.split('/').pop());
    } catch (e) {
      return (src || '').split('?')[0].split('/').pop();
    }
  }

  // Absolute src URLs of all content images inside root (deduped, DOM order).
  function collectImages(root) {
    if (!root || !root.querySelectorAll) return [];
    const out = [];
    root.querySelectorAll('img').forEach((img) => {
      if (!isContentImage(img)) return;
      const src = img.currentSrc || img.src;
      if (!out.includes(src)) out.push(src);
    });
    return out;
  }

  function getCleanText(el) {
    const ourBtn = document.getElementById('tb-error-logger-btn');
    const wasHidden = ourBtn && ourBtn.style.display === 'none';
    if (ourBtn) ourBtn.style.display = 'none';

    // Include bare native MathML <math> — Testbook renders some option fractions as
    // raw MathML (no KaTeX wrapper), and without this they flatten to "1578255".
    const MATH_SEL = '.katex, .MathJax_Display, .MathJax, mjx-container, math';
    const mathReplacements = [];
    el.querySelectorAll(MATH_SEL).forEach((math) => {
      // Only process outermost math nodes; skip descendants of already-matched nodes
      // (e.g. the hidden <math> inside a .katex is covered by the .katex itself).
      if (math.parentElement && math.parentElement.closest(MATH_SEL)) return;

      let text = '';
      // KaTeX stores the original LaTeX in <annotation encoding="application/x-tex">
      const annotation = math.querySelector('annotation[encoding="application/x-tex"]');
      if (annotation) {
        text = annotation.textContent.trim();
      } else {
        // Fallback: structurally serialize the MathML so fractions keep their bar.
        const mathmlEl = math.matches('math') ? math : math.querySelector('.katex-mathml math, math');
        if (mathmlEl) text = mathmlToText(mathmlEl).trim();
      }

      if (text) {
        const ph = document.createElement('span');
        ph.textContent = ' ' + text + ' ';
        math.parentNode.insertBefore(ph, math);
        math.style.display = 'none';
        mathReplacements.push({ math, ph });
      }
    });

    // Images contribute nothing to innerText, so a figure-based question reads as
    // bare instruction text (and image-only options read as empty). Temporarily
    // insert an "[Image: filename]" span before each content image so the text
    // keeps the figure's position — this also makes the dedupe hash unique across
    // different figure questions that share identical instruction text.
    const imgPlaceholders = [];
    el.querySelectorAll('img').forEach((img) => {
      if (!isContentImage(img) || !img.parentNode) return;
      const ph = document.createElement('span');
      ph.textContent = ' [Image: ' + imageName(img.currentSrc || img.src) + '] ';
      img.parentNode.insertBefore(ph, img);
      imgPlaceholders.push(ph);
    });

    const txt = (el.innerText || '').trim();

    imgPlaceholders.forEach((ph) => ph.remove());
    mathReplacements.forEach(({ math, ph }) => {
      math.style.display = '';
      ph.remove();
    });

    if (ourBtn && !wasHidden) ourBtn.style.display = '';
    return txt;
  }

  function extractAllQuestions() {
    // Strategy 1: Platform-specific question card selectors
    const selectors = PLATFORM_SELECTORS[PLATFORM] || PLATFORM_SELECTORS.testbook;
    let cards = [];
    for (const sel of selectors) {
      const found = document.querySelectorAll(sel);
      if (found.length > 0) { cards = Array.from(found); break; }
    }

    // Strategy 2: Keyword-based fallback — find the tightest div that
    // contains "correct answer" AND has question content (a "?" or numbered
    // options) before it. This prevents grabbing just the solution section.
    if (cards.length === 0) {
      const allDivs = Array.from(document.querySelectorAll('div')).filter(
        (d) => d.id !== 'tb-error-logger-btn' && !document.getElementById('tb-error-logger-btn')?.contains(d)
      );
      const candidates = allDivs.filter((d) => {
        const txt = getCleanText(d);
        const caIdx = txt.search(/correct answer/i);
        if (caIdx < 0 || txt.length > 8000) return false;
        const preCa = txt.slice(0, caIdx);
        // Must have a "?" (question) or numbered options (1 | / 1. / A) B))
        // before "correct answer" — filters out solution-only containers
        return (
          preCa.length > 50 &&
          (/\?/.test(preCa) || /\b[1-4]\s*[|\.:]/.test(preCa) || /^[A-D][\s\)\.]/m.test(preCa))
        );
      });
      // Pick the smallest (most specific) matching container
      if (candidates.length > 0) {
        candidates.sort((a, b) => getCleanText(a).length - getCleanText(b).length);
        cards = [candidates[0]];
      }
    }

    // Strategy 3 (PW DPP): locate cursor-default option divs and walk up to the smallest
    // ancestor that contains all of them — that is the question card.
    if (PLATFORM === 'pw' && cards.length === 0) {
      const dppOpts = Array.from(document.querySelectorAll('div[class*="cursor-default"]'))
        .filter((el) => {
          const t = (el.innerText || '').trim();
          return t.length > 5 && t.length < 600 && !el.querySelector('[class*="htmlContent-"]');
        });
      if (dppOpts.length >= 2) {
        let candidate = dppOpts[0].parentElement;
        while (candidate && candidate !== document.body) {
          if (dppOpts.every((opt) => candidate.contains(opt))) break;
          candidate = candidate.parentElement;
        }
        if (candidate && candidate !== document.body) cards = [candidate];
      }
    }

    // Strategy 4 (Testbook single-question review mode): "Your answer / Correct answer"
    // lives in a fixed header panel SEPARATE from the question body, so Strategy 2's
    // co-location check always fails. Find the smallest div that has ≥1 htmlContent-*
    // descendant AND is NOT a sticky answer panel.
    if (PLATFORM === 'testbook' && cards.length === 0) {
      const ourBtn = document.getElementById('tb-error-logger-btn');
      const withHtml = Array.from(document.querySelectorAll('div')).filter((d) => {
        if (d === ourBtn || ourBtn?.contains(d)) return false;
        if (d.querySelectorAll('[class*="htmlContent-"]').length < 1) return false;
        // Exclude sticky answer header panels — their text starts with answer-display labels
        const early = (d.textContent || '').slice(0, 200).toLowerCase();
        if (/your\s+answer|correct\s+answer|accepted\s+answer/.test(early)) return false;
        return true;
      });
      if (withHtml.length > 0) {
        withHtml.sort((a, b) => getCleanText(a).length - getCleanText(b).length);
        const best3 = withHtml.find((d) => {
          const len = getCleanText(d).length;
          return len >= 80 && len <= 6000;
        });
        if (best3) cards = [best3];
      }
    }

    return cards;
  }

  function classifyQuestion(card) {
    const txt = getCleanText(card).toLowerCase();
    // Oliveboard semantic option classes: .opt.wrong = user's incorrect pick, .opt.correct = right answer.
    if (PLATFORM === 'oliveboard') {
      if (card.querySelector('.opt.wrong')) return 'wrong';
      if (card.querySelector('.opt.correct')) return 'correct';
    }
    if (/incorrect|wrong answer|you chose the wrong/i.test(txt)) return 'wrong';
    // PW shows "Status\nSkipped" or "Status Skipped"
    if (/\bskipped\b|unattempted|not attempted/i.test(txt)) return 'skipped';
    // Old Testbook Angular UI: semantic option classes are the ground truth.
    // incorrect-option = what the user picked (wrong). correct-option = right answer.
    if (PLATFORM === 'testbook') {
      if (card.querySelector('li.option.incorrect-option')) return 'wrong';
      if (card.querySelector('li.option.correct-option, li.option.stat-available')) return 'correct';
    }
    // Tailwind color highlight = wrong/correct. Applies to both Testbook and PW (same color system).
    // Must check BEFORE the bare "correct" text check below.
    if (PLATFORM === 'testbook' || PLATFORM === 'pw') {
      if (card.querySelector('[class*="B91C1C"], [class*="FEE2E2"], [class*="EF4444"], [class*="BF2734"], [class*="FEE7E9"]')) return 'wrong';
      if (card.querySelector('[class*="1B7938"], [class*="DFF1E4"], [class*="4CAF50"]')) return 'correct';
    }
    if (/correct|you answered correctly/i.test(txt)) return 'correct';
    if (card.querySelector('[class*="wrong"], [class*="incorrect"], .fa-times, .icon-wrong')) return 'wrong';
    if (card.querySelector('[class*="correct"], [class*="right"], .fa-check')) return 'correct';
    return 'unknown';
  }

  // If "Correct Answer" is a bare number ("3") or letter ("B"), resolve to option text.
  function resolveNumberedAnswer(text, options) {
    const trimmed = (text || '').trim();
    // Numeric: "3" → options[2]
    const num = parseInt(trimmed, 10);
    if (!isNaN(num) && String(num) === trimmed && num >= 1 && num <= options.length) {
      return options[num - 1];
    }
    // Letter: "A", "B", "C", "D" (optionally followed by ) or .)
    const letterM = trimmed.match(/^([A-D])[\s\)\.]*$/i);
    if (letterM && options.length > 0) {
      const idx = letterM[1].toUpperCase().charCodeAt(0) - 65;
      if (idx >= 0 && idx < options.length) return options[idx];
    }
    return trimmed;
  }

  function extractQuestionData(card) {
    const rawText = getCleanText(card);

    // Clone for raw_html so our button markup isn't in it
    const clone = card.cloneNode(true);
    clone.querySelectorAll('#tb-error-logger-btn, .tb-el-toast').forEach((n) => n.remove());
    const rawHtml = clone.innerHTML;

    // Find solution section first so we can exclude it from options
    const solutionSelectors = [
      '[class*="solution"]', '[class*="Solution"]',
      '[class*="explanation"]', '[class*="Explanation"]',
      '.sol-text', '[class*="sol-"]',
      // Old Testbook Angular solution containers
      '[class*="que-sol"]', '[class*="view-sol"]',
      '[class*="sol-content"]', '[class*="solContent"]',
    ];
    const solutionNodes = new Set();
    for (const sel of solutionSelectors) {
      card.querySelectorAll(sel).forEach((n) => solutionNodes.add(n));
    }
    // Remove child solution nodes that are already covered by a parent
    const solutionRoots = Array.from(solutionNodes).filter(
      (n) => !Array.from(solutionNodes).some((p) => p !== n && p.contains(n))
    );
    // Old Testbook Angular: [class*="solution"] matches "solution-like-box" (a "Was this
    // helpful?" feedback widget whose text is ~35 chars). The real solution is in
    // div.qns-view-box.ng-binding that is NOT the question (no mar-b16) and NOT inside
    // a li (not an option text box). Trigger when nothing meaningful found yet.
    const _preSolText = solutionRoots.map((n) => (n.innerText || '').trim()).join('');
    if (PLATFORM === 'testbook' && _preSolText.length < 50) {
      const angSol = Array.from(card.querySelectorAll('div.qns-view-box.ng-binding'))
        .filter((el) =>
          !el.classList.contains('mar-b16') &&
          !el.closest('li') &&
          (el.innerText || '').length > 50
        )
        .sort((a, b) => (b.innerText || '').length - (a.innerText || '').length)[0];
      if (angSol) {
        solutionRoots.length = 0;  // discard the feedback-widget false positives
        solutionRoots.push(angSol);
      }
    }
    // PW DPP: solution always appears AFTER the answer options in the DOM.
    // Find it by locating the last cursor-default option, then taking the first
    // substantial text div that follows it (and is not itself an option).
    const _preSolTextPw = solutionRoots.map((n) => (n.innerText || '').trim()).join('');
    if (PLATFORM === 'pw' && _preSolTextPw.length < 20) {
      const searchRoot = card.parentElement || card;
      const dppOptEls = Array.from(searchRoot.querySelectorAll('[class*="cursor-default"]'))
        .filter((el) => (el.innerText || '').trim().length > 5);
      if (dppOptEls.length > 0) {
        const allDivs = Array.from(searchRoot.querySelectorAll('div'));
        const lastOptIdx = allDivs.indexOf(dppOptEls[dppOptEls.length - 1]);
        const solCandidates = (lastOptIdx >= 0 ? allDivs.slice(lastOptIdx + 1) : allDivs)
          .filter((el) => {
            if (dppOptEls.some((o) => o === el || o.contains(el))) return false;
            const t = (el.innerText || '').trim();
            return t.length > 80 && t.length < 5000 && el.children.length < 30;
          })
          .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
        if (solCandidates[0]) {
          solutionRoots.length = 0;
          solutionRoots.push(solCandidates[0]);
        }
      }
    }
    const solutionText = solutionRoots.map((n) => getCleanText(n)).filter(Boolean).join('\n\n');

    const isInsideSolution = (el) => solutionRoots.some((s) => s.contains(el));

    // Options: Old Testbook Angular shortcut — real MCQ option LIs always carry
    // the ng-scope class AND have a .qns-view-box child. Stat-only LIs (e.g.
    // "35% answered correctly") and numerical display LIs lack ng-scope entirely.
    // Using ng-scope as a filter avoids picking up fake stat elements.
    const _angularPairs = PLATFORM === 'testbook'
      ? Array.from(card.querySelectorAll('li.option.ng-scope'))
          .filter((li) => !isInsideSolution(li) && li.querySelector('.qns-view-box'))
          .map((li) => {
            const box = li.querySelector('.qns-view-box');
            return { text: getCleanText(box), el: box };
          })
          .filter((p) => p.text)
      : [];
    const _angularOpts = _angularPairs.map((p) => p.text);

    // If Angular options found, skip the broad generic selector entirely.
    const optionEls = _angularOpts.length > 0 ? [] : Array.from(
      card.querySelectorAll(
        '[class*="option"], [class*="Option"], [class*="choice"], [class*="Choice"], ' +
        '[class*="answer-opt"], [class*="answerOpt"], [class*="AnswerOpt"], ' +
        '[class*="optionText"], [class*="option-text"], [class*="option-label"], ' +
        '[class*="answerText"], [class*="answer-text"], [class*="answerOption"], ' +
        '[data-option-id], [data-choice], [data-option], [data-answer-index], .opt'
      )
    ).filter((el) => !isInsideSolution(el));

    // Source element behind each options[i] (null when only text is known) —
    // used at the end to collect per-option images, aligned by index.
    const optionSrcEls = _angularPairs.slice(0, 8).map((p) => p.el);
    const options = _angularPairs.slice(0, 8).map((p) => p.text);
    if (options.length === 0) {
      optionEls.slice(0, 8).forEach((o) => {
        const innerBox = o.querySelector && o.querySelector('.qns-view-box');
        const t = getCleanText(innerBox || o);
        if (t) {
          options.push(t);
          optionSrcEls.push(innerBox || o);
        }
      });
    }

    // Fallback 1: li/items inside explicit option wrappers
    if (options.length === 0) {
      card.querySelectorAll(
        '[class*="options"], [class*="Options"], [class*="choices"], [class*="Choices"]'
      ).forEach((wrap) => {
        if (isInsideSolution(wrap)) return;
        wrap.querySelectorAll('li, [class*="item"]').forEach((li) => {
          if (!isInsideSolution(li)) {
            const t = getCleanText(li);
            if (t) {
              options.push(t);
              optionSrcEls.push(li);
            }
          }
        });
      });
      options.splice(8);
      optionSrcEls.splice(8);
    }

    // Fallback 2: radio/checkbox label text (works on many quiz platforms)
    if (options.length === 0) {
      card.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((input) => {
        if (isInsideSolution(input)) return;
        const label =
          input.closest('label') ||
          (input.id && document.querySelector(`label[for="${input.id}"]`)) ||
          input.parentElement;
        if (label && !isInsideSolution(label)) {
          const t = getCleanText(label);
          if (t && t.length < 500 && !options.includes(t)) {
            options.push(t);
            optionSrcEls.push(label);
          }
        }
      });
      options.splice(8);
      optionSrcEls.splice(8);
    }

    // Fallback 3: parse "A) ...", "B) ..." lines from raw text
    if (options.length === 0) {
      const optMatches = rawText.match(/^[A-D]\s*[\)\.]\s*.+/gm);
      if (optMatches && optMatches.length >= 2) {
        optMatches.forEach((m) => {
          options.push(m.trim());
          optionSrcEls.push(null); // parsed from text — no element to pull images from
        });
        options.splice(8);
        optionSrcEls.splice(8);
      }
    }

    // Question text
    let questionEl =
      card.querySelector('[class*="question-text"], [class*="QuestionText"], .q-text, .question-stmt') ||
      card.querySelector('[class*="ques-text"], [class*="quesText"], [class*="questionText"]') ||
      // Old Testbook Angular: question text has 'mar-b16 qns-view-box' (options use 'qns-view-box' without mar-b16)
      card.querySelector('.mar-b16.qns-view-box');

    // New Testbook React: content rendered via htmlContent-* classes.
    // Sort by text length desc — question body is longer than sticky-header snippets.
    if (!questionEl && PLATFORM === 'testbook') {
      const htmlContents = Array.from(card.querySelectorAll('[class*="htmlContent-"]'))
        .filter((el) =>
          !isInsideSolution(el) &&
          !el.closest('div[class*="cursor-default"]') &&
          !el.closest('[class*="option"], [class*="Option"]')
        );
      htmlContents.sort((a, b) => (b.innerText || '').length - (a.innerText || '').length);
      if (htmlContents.length > 0) questionEl = htmlContents[0];
    }

    if (!questionEl) questionEl = card.querySelector('p');

    // Extract correct/your answer from raw text using regex
    const correctM = rawText.match(/correct\s*answer\s*[:\-]?\s*([^\n]{1,120})/i);
    const yourM = rawText.match(/(?:your\s*answer|you\s*(?:chose|selected)|attempted)\s*[:\-]?\s*([^\n]{1,120})/i);

    let correctRaw = correctM ? correctM[1].trim() : '';
    let yourRaw = yourM ? yourM[1].trim() : '';

    // Testbook writes "Correct answer is option (N)." — regex captures "is option (1)."
    // Extract the option number so resolveNumberedAnswer maps it to actual option text.
    const isOptM = correctRaw.match(/^is\s+option\s*\(?(\d+)\)?\.?$/i);
    if (isOptM) {
      correctRaw = String(parseInt(isOptM[1], 10));
    } else if (/^is\s+(?!option)/i.test(correctRaw)) {
      // Old Testbook Angular solution text: "∴ The correct answer is ₹24582."
      // Regex captures "is ₹24582." — strip the leading "is " and trailing period.
      correctRaw = correctRaw.replace(/^is\s+/i, '').replace(/\.\s*$/, '').trim();
    }

    // Old Testbook Angular: DOM class-based answer detection (confirmed from F12 inspection).
    // correct-option = right answer, incorrect-option = what user selected (wrong).
    // MUST use ng-scope qualifier: stat-only LIs (e.g. "35% answered correctly") also
    // carry correct-option/stat-available but lack ng-scope. Without the qualifier,
    // the stat LI would be returned as the correct answer text.
    if (PLATFORM === 'testbook') {
      const optText = (li) => {
        const inner = li.querySelector('.qns-view-box');
        return getCleanText(inner || li);
      };
      if (!correctRaw) {
        const correctLi = card.querySelector('li.option.ng-scope.correct-option')
          || card.querySelector('li.option.ng-scope.stat-available');
        if (correctLi) correctRaw = optText(correctLi);
      }
      if (!yourRaw) {
        const yourLi = card.querySelector('li.option.ng-scope.incorrect-option');
        if (yourLi) yourRaw = optText(yourLi);
      }
    }

    // Numerical type questions show "Accepted answer is between: X and Y" instead
    // of a standard "Correct answer: X" label.
    if (!correctRaw) {
      const numM = rawText.match(
        /accepted\s+answer\s+is\s+between\s*:?\s*([\d.]+)\s+and\s+([\d.]+)/i
      );
      if (numM) correctRaw = numM[1] === numM[2] ? numM[1] : `${numM[1]}–${numM[2]}`;
    }

    // Tailwind arbitrary-value color classes on option containers — used by both Testbook and PW.
    if (PLATFORM === 'testbook' || PLATFORM === 'pw') {
      const GREEN = ['1B7938', 'DFF1E4', '4CAF50'];
      const RED   = ['B91C1C', 'FEE2E2', 'EF4444', 'FF0000', 'BF2734', 'FEE7E9'];

      // cursor-default divs with htmlContent- children = Testbook/standard DPP format
      const tbOptionDivs = Array.from(
        card.querySelectorAll('div[class*="cursor-default"]')
      ).filter((el) => !isInsideSolution(el) && el.querySelector('[class*="htmlContent-"]'));

      // PW DPP format: cursor-default divs with text directly (no htmlContent- wrapper).
      // Text starts with the option number + newlines e.g. "2\n\nOption text..." — strip prefix.
      const stripOptNum = (t) => t.replace(/^\d+\s*\n+/, '').trim();
      const pwDppOptionDivs = tbOptionDivs.length === 0
        ? Array.from(card.querySelectorAll('div[class*="cursor-default"]')).filter((el) => {
            if (isInsideSolution(el)) return false;
            if (el.querySelector('[class*="htmlContent-"]')) return false;
            const t = (el.innerText || '').trim();
            return t.length > 5 && t.length < 600;
          })
        : [];

      if (tbOptionDivs.length > 0) {
        if (options.length === 0) {
          tbOptionDivs.forEach((container) => {
            const textEl = container.querySelector('[class*="htmlContent-"]');
            const t = textEl ? getCleanText(textEl) : '';
            if (t) {
              options.push(t);
              optionSrcEls.push(container);
            }
          });
          options.splice(8);
          optionSrcEls.splice(8);
        }
        tbOptionDivs.forEach((container) => {
          const cls = container.className;
          const textEl = container.querySelector('[class*="htmlContent-"]');
          const t = textEl ? getCleanText(textEl) : '';
          if (!t) return;
          if (GREEN.some((c) => cls.includes(c))) correctRaw = t;
          if (RED.some((c) => cls.includes(c))) yourRaw = t;
        });
      } else if (pwDppOptionDivs.length > 0) {
        if (options.length === 0) {
          pwDppOptionDivs.forEach((container) => {
            const t = stripOptNum(getCleanText(container));
            if (t) {
              options.push(t);
              optionSrcEls.push(container);
            }
          });
          options.splice(8);
          optionSrcEls.splice(8);
        }
        pwDppOptionDivs.forEach((container) => {
          const cls = container.className;
          const t = stripOptNum(getCleanText(container));
          if (!t) return;
          if (GREEN.some((c) => cls.includes(c))) correctRaw = t;
          if (RED.some((c) => cls.includes(c))) yourRaw = t;
        });
      } else {
        // No cursor-default wrappers — colors applied directly on option/answer elements.
        const colorSel = GREEN.concat(RED).map((c) => `[class*="${c}"]`).join(',');
        Array.from(card.querySelectorAll(colorSel))
          .filter((el) => !isInsideSolution(el))
          .forEach((el) => {
            const cls = el.className || '';
            const textEl = el.querySelector('[class*="htmlContent-"]') || el;
            const t = getCleanText(textEl);
            if (!t || t.length > 300) return;
            if (GREEN.some((c) => cls.includes(c)) && !correctRaw) correctRaw = t;
            if (RED.some((c) => cls.includes(c)) && !yourRaw) yourRaw = t;
          });
      }
    }

    // ---- Images (figures in question / options / solution) ----
    const solutionImages = [];
    solutionRoots.forEach((n) => {
      collectImages(n).forEach((src) => {
        if (!solutionImages.includes(src)) solutionImages.push(src);
      });
    });
    const optionImages = optionSrcEls.map((el) => (el ? collectImages(el) : []));
    const questionImages = questionEl ? collectImages(questionEl) : [];
    // Figures often sit OUTSIDE the detected question-text element (e.g. directly
    // under the question wrapper). Claim any card image not already attributed to
    // the question, an option, or the solution as a question image.
    const claimed = new Set([...questionImages, ...solutionImages]);
    optionImages.forEach((arr) => arr.forEach((src) => claimed.add(src)));
    card.querySelectorAll('img').forEach((img) => {
      if (!isContentImage(img)) return;
      const src = img.currentSrc || img.src;
      if (claimed.has(src)) return;
      if (isInsideSolution(img)) return;
      if (img.closest('li.option')) return;
      if (optionSrcEls.some((el) => el && el.contains && el.contains(img))) return;
      questionImages.push(src);
      claimed.add(src);
    });

    return {
      raw_text: rawText,
      raw_html: rawHtml,
      question_text: questionEl ? getCleanText(questionEl) : '',
      options,
      // Resolve bare numbers → option text so AI gets "Uttar Pradesh" not "3"
      correct_answer_hint: resolveNumberedAnswer(correctRaw, options),
      your_answer_hint: resolveNumberedAnswer(yourRaw, options),
      solution_text: solutionText,
      status: classifyQuestion(card),
      question_images: questionImages,
      option_images: optionImages,
      solution_images: solutionImages,
    };
  }

  function detectSessionType() {
    const url = window.location.href.toLowerCase();
    if (/dpp/.test(url)) return 'dpps';
    return 'mock';
  }

  function extractTestMeta() {
    let title;
    if (PLATFORM === 'oliveboard') {
      // Solution app puts the test name in a centered header div.
      const h = document.querySelector('.header-col.text-center, .header-col');
      title = h && h.innerText.trim() ? h.innerText.trim() : document.title;
    } else {
      const titleEl = document.querySelector('h1, h2, [class*="test-title"], [class*="TestTitle"], [class*="quiz-title"], [class*="quizTitle"]');
      title = titleEl ? titleEl.innerText.trim() : document.title;
    }
    const platformLabel = { testbook: 'Testbook', pw: 'PW', oliveboard: 'Oliveboard', testranking: 'TestRanking' }[PLATFORM] || PLATFORM;
    const sessionType = detectSessionType();

    return {
      test_title: `[${platformLabel}][${sessionType.toUpperCase()}] ${title}`.slice(0, 200),
      test_url: window.location.href,
      captured_at: new Date().toISOString(),
      platform: PLATFORM,
      session_type: sessionType,
    };
  }

  // ---------- Oliveboard solution-app scraping ----------
  // Oliveboard renders one question at a time. All 100 question blocks
  // (.qosblock.singleqid) exist in the DOM, but only the currently-navigated one is
  // display:flex AND decoded — the others are display:none with encoded placeholder
  // text. Content scripts run in an isolated world and can't call the page's
  // goToQuestion(), but clicking a palette cell (which carries inline
  // onclick="goToQuestion(N)") runs the handler in the page world and decodes the
  // target question. So we walk the answer-map, clicking each wrong/unattempted cell,
  // wait for it to decode, and scrape the now-visible block.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function getVisibleOliveBlock() {
    return Array.from(document.querySelectorAll('.qosblock.singleqid')).find(
      (b) => b.offsetParent !== null && getComputedStyle(b).display !== 'none'
    );
  }

  // A block is ready when it's the one we asked for (id "q-<idx>-...") and its options
  // have been decoded into .eqt spans (encoded blocks have no .eqt children).
  function oliveBlockReady(block, idx) {
    return (
      block &&
      block.id &&
      block.id.indexOf('q-' + idx + '-') === 0 &&
      block.querySelector('.opt .eqt')
    );
  }

  function oliveOptText(optEl) {
    // .eqt holds the visible text; .hqt is a display:none duplicate (innerText skips it).
    const target = optEl.querySelector('.rightopt') || optEl.querySelector('.right') || optEl;
    return getCleanText(target);
  }

  function extractOliveboardBlock(block, paletteStatus) {
    // The ".toppervsyou" block is a You-vs-Topper TIMING table ("You … 37  Topper … 22").
    // It must be stripped from raw_text/raw_html — otherwise the server's answer-extraction
    // heuristic grabs a time number (e.g. 37) as the "your answer", which is especially wrong
    // for unattempted questions (no answer was given at all).
    const clone = block.cloneNode(true);
    clone
      .querySelectorAll('#tb-error-logger-btn, .tb-el-toast, .toppervsyou')
      .forEach((n) => n.remove());

    const qEl = block.querySelector('.qblock');
    const optEls = Array.from(block.querySelectorAll('.opt'));
    const options = optEls.map(oliveOptText).filter(Boolean).slice(0, 8);

    const correctEl = block.querySelector('.opt.correct');
    const wrongEl = block.querySelector('.opt.wrong'); // the option the user picked (incorrect)
    const correct = correctEl ? oliveOptText(correctEl) : '';
    const your = wrongEl ? oliveOptText(wrongEl) : '';

    const solEl = block.querySelector('.solutiontxt') || block.querySelector('.sblock');
    let solution = solEl ? getCleanText(solEl) : '';
    solution = solution.replace(/^\s*Solution\s*/i, '').trim();

    let status = 'unknown';
    if (wrongEl) status = 'wrong';
    else if (paletteStatus === 'unattempted') status = 'skipped';
    else if (correctEl) status = 'correct';

    // Build raw_text from the question + labelled options + answers + solution rather than
    // dumping the whole block, so the timing table can't leak and the server gets clean signal.
    const rawParts = [];
    if (qEl) rawParts.push(getCleanText(qEl));
    if (options.length) {
      rawParts.push(options.map((o, i) => `${String.fromCharCode(65 + i)}) ${o}`).join('\n'));
    }
    if (your) rawParts.push('Your answer: ' + your);
    if (correct) rawParts.push('Correct answer: ' + correct);
    if (solution) rawParts.push('Solution: ' + solution);

    return {
      raw_text: rawParts.join('\n'),
      raw_html: clone.innerHTML,
      question_text: qEl ? getCleanText(qEl) : '',
      options,
      correct_answer_hint: correct,
      your_answer_hint: your,
      solution_text: solution,
      status,
      question_images: qEl ? collectImages(qEl) : [],
      option_images: optEls.map((o) => collectImages(o)),
      solution_images: solEl ? collectImages(solEl) : [],
    };
  }

  // Walk the answer-map palette and collect every wrong + unattempted question.
  async function collectOliveboard(onProgress) {
    const cells = Array.from(document.querySelectorAll('.map-qno'));
    const idxOf = (c) => {
      const k = Array.from(c.classList).find((x) => /^q-\d+$/.test(x));
      return k ? parseInt(k.slice(2), 10) : null;
    };

    const targets = [];
    cells.forEach((c) => {
      const idx = idxOf(c);
      if (idx === null) return;
      if (c.classList.contains('wrong')) targets.push({ idx, cell: c, status: 'wrong' });
      else if (c.classList.contains('unattempted')) targets.push({ idx, cell: c, status: 'unattempted' });
    });
    if (targets.length === 0) return [];

    // Remember where the user was so we can return them there afterward.
    const activeCell = cells.find((c) => c.classList.contains('activeq'));
    const originalIdx = activeCell ? idxOf(activeCell) : null;

    const results = [];
    for (let i = 0; i < targets.length; i++) {
      const t = targets[i];
      if (onProgress) onProgress(i + 1, targets.length);
      t.cell.click();
      let block = null;
      for (let tries = 0; tries < 40; tries++) {
        await sleep(100);
        block = getVisibleOliveBlock();
        if (oliveBlockReady(block, t.idx)) break;
      }
      if (block) results.push(extractOliveboardBlock(block, t.status));
    }

    // Restore the user's original question.
    if (originalIdx !== null) {
      const back = cells.find((c) => idxOf(c) === originalIdx);
      if (back) back.click();
    }
    return results;
  }

  async function handleScanOliveboard() {
    if (!document.querySelector('.map-qno')) {
      toast('Open the question-wise Solutions view first (click "View Solutions").', 'warn');
      return;
    }
    let progressToast = null;
    const showProgress = (done, total) => {
      const msg = `Reading Oliveboard solutions… ${done}/${total}`;
      if (!progressToast) {
        progressToast = document.createElement('div');
        progressToast.className = 'tb-el-toast tb-el-toast-info show';
        document.body.appendChild(progressToast);
      }
      progressToast.textContent = msg;
    };

    try {
      const questions = (await collectOliveboard(showProgress)).filter(
        (q) => q.status === 'wrong' || q.status === 'skipped'
      );
      if (progressToast) progressToast.remove();

      if (questions.length === 0) {
        toast('No wrong or skipped questions found in this test.', 'warn');
        return;
      }
      await sendToServer({ meta: extractTestMeta(), questions }, questions.length);
    } catch (err) {
      if (progressToast) progressToast.remove();
      toast(`❌ ${err.message}`, 'error');
    }
  }

  // ---------- TestRanking (API-based capture) ----------
  // The solution page (/user/solution-details-pt/<testId>) is a Next.js SPA fed by
  // two same-origin JSON APIs, so instead of scraping the DOM (which renders one
  // question at a time) we fetch everything in one shot. Content-script fetch()
  // is same-origin here, so the user's session cookies are sent automatically.

  const TR_SOLUTION_RE = /^\/user\/solution-details-pt\/(\d+)/;

  function trTestId() {
    const m = window.location.pathname.match(TR_SOLUTION_RE);
    return m ? m[1] : null;
  }

  function trUserId() {
    try {
      return JSON.parse(localStorage.getItem('user')).user_id || null;
    } catch (e) {
      return null;
    }
  }

  async function trFetchData(path) {
    const r = await fetch(window.location.origin + path);
    if (!r.ok) throw new Error(`TestRanking API error (HTTP ${r.status}) — are you logged in?`);
    const j = await r.json();
    if (!j || !Array.isArray(j.data)) throw new Error('Unexpected TestRanking API response');
    return j.data;
  }

  // Convert an API HTML string (question_en / option_en_N / solution_en) to
  // {text, images[]} using a detached document. MathJax TeX like \(\frac{a}{b}\)
  // lives in plain text nodes in the source HTML, so it survives verbatim (the
  // dashboard's mathify() renders it). <img> tags become the same
  // "[Image: name]" tokens getCleanText produces, keeping figure position and
  // making the dedupe hash unique across figure questions with identical text.
  function trHtmlToParts(html) {
    if (!html || !String(html).trim()) return { text: '', images: [] };
    const doc = new DOMParser().parseFromString(String(html), 'text/html');
    const images = [];
    doc.querySelectorAll('img').forEach((img) => {
      const raw = img.getAttribute('src') || '';
      if (!raw || raw.startsWith('data:')) { img.remove(); return; }
      let abs;
      try { abs = new URL(raw, window.location.origin).href; } catch (e) { img.remove(); return; }
      if (!images.includes(abs)) images.push(abs);
      img.replaceWith(doc.createTextNode(' [Image: ' + imageName(abs) + '] '));
    });
    doc.querySelectorAll('br').forEach((br) => br.replaceWith('\n'));
    doc.querySelectorAll('p, div, li, tr, h1, h2, h3, h4, table').forEach((el) => el.append('\n'));
    const text = (doc.body.textContent || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
    return { text, images };
  }

  function trAbsImage(v) {
    if (!v || !String(v).trim()) return null;
    try { return new URL(String(v).trim(), window.location.origin).href; } catch (e) { return null; }
  }

  function trBuildSourceUrl(qNum, sectionName) {
    let hash = '#tbel-q=' + qNum;
    if (sectionName) hash += '&tbel-sn=' + encodeURIComponent(sectionName);
    return window.location.origin + window.location.pathname + hash;
  }

  async function collectTestranking() {
    const testId = trTestId();
    if (!testId) throw new Error('Not a solution page');
    const userId = trUserId();
    if (!userId) throw new Error('Could not read your TestRanking user id — are you logged in?');

    const [solSections, userSections] = await Promise.all([
      trFetchData(`/admin/api/questions-solutions-new/${testId}/en`),
      trFetchData(`/admin/api/user-solution-new/${userId}/${testId}/1`),
    ]);

    // Per-section user results, keyed by section_id
    const userBySec = {};
    userSections.forEach((s) => {
      userBySec[String(s.section_id)] = {
        incorrect: new Set((s.incorrect_ids || []).map(String)),
        ans: new Map((s.ansData || []).map((a) => [String(a.q_id), a])),
      };
    });

    const questions = [];
    let seriesName = '';
    const multiSection = solSections.length > 1;

    solSections.forEach((sec) => {
      seriesName = seriesName || sec.series_name || '';
      const secKey = String(sec.section_id);
      const qlist =
        (sec.all_questions && (sec.all_questions[secKey] || Object.values(sec.all_questions)[0])) || [];
      const u = userBySec[secKey] || { incorrect: new Set(), ans: new Map() };

      qlist.forEach((q, i) => {
        const qid = String(q.qid);
        const qNum = i + 1; // palette order (per section)
        let status;
        if (u.incorrect.has(qid)) status = 'wrong';
        else if (!u.ans.has(qid)) status = 'skipped';
        else return; // answered correctly — not captured

        // Options keep their original 1-based slot numbers so answer_en / q_ans
        // indices stay valid even when a middle slot is empty.
        const slots = [];
        for (let k = 1; k <= 5; k++) {
          const parts = trHtmlToParts(q['option_en_' + k]);
          const extraImg = trAbsImage(q['option_en_image' + k]);
          if (extraImg && !parts.images.includes(extraImg)) {
            parts.images.push(extraImg);
            if (!parts.text) parts.text = '[Image: ' + imageName(extraImg) + ']';
          }
          if (parts.text || parts.images.length) slots.push({ num: k, text: parts.text, images: parts.images });
        }
        const bySlot = (n) => slots.find((s) => s.num === n);

        const correctSlot = bySlot(parseInt(q.answer_en, 10));
        // Non-MCQ questions (numeric entry): answer_en holds the literal answer.
        const correctHint = correctSlot ? correctSlot.text : trHtmlToParts(q.answer_en).text;

        let yourHint = '';
        if (status === 'wrong') {
          const a = u.ans.get(qid);
          if (a) {
            const chosen = bySlot(parseInt(a.q_ans, 10));
            yourHint = chosen ? chosen.text : String(a.q_ans || '');
          }
        }

        const compParts = trHtmlToParts(q.comprehensive_en); // RC passage, usually empty
        const qParts = trHtmlToParts(q.question_en);
        const qImgField = trAbsImage(q.question_en_image);
        if (qImgField && !qParts.images.includes(qImgField)) qParts.images.push(qImgField);
        const solParts = trHtmlToParts(q.solution_en);

        const questionText = [compParts.text, qParts.text].filter(Boolean).join('\n\n');
        const options = slots.map((s) => s.text || '[Image: option ' + s.num + ']');

        // raw_text is built deterministically from API fields, so the server's
        // sha256 dedupe makes re-scans free.
        const rawParts = [questionText];
        if (options.length) {
          rawParts.push(options.map((o, j) => String.fromCharCode(65 + j) + ') ' + o).join('\n'));
        }
        if (yourHint) rawParts.push('Your answer: ' + yourHint);
        if (correctHint) rawParts.push('Correct answer: ' + correctHint);
        if (solParts.text) rawParts.push('Solution: ' + solParts.text);

        questions.push({
          raw_text: rawParts.filter(Boolean).join('\n'),
          raw_html: [q.comprehensive_en, q.question_en, ...slots.map((s) => q['option_en_' + s.num]), q.solution_en]
            .filter(Boolean)
            .join('\n<hr>\n')
            .slice(0, 20000),
          question_text: questionText,
          options,
          correct_answer_hint: correctHint,
          your_answer_hint: yourHint,
          solution_text: solParts.text,
          status,
          question_images: [...compParts.images, ...qParts.images],
          option_images: slots.map((s) => s.images),
          solution_images: solParts.images,
          source_url: trBuildSourceUrl(qNum, multiSection ? sec.section_name || '' : ''),
        });
      });
    });

    return { questions, seriesName };
  }

  async function handleScanTestranking() {
    if (!trTestId()) {
      toast('Open a solution page first (…/user/solution-details-pt/<id>).', 'warn');
      return;
    }
    toast('Fetching solutions from TestRanking…', 'info');
    try {
      const { questions, seriesName } = await collectTestranking();
      if (questions.length === 0) {
        toast('No wrong or skipped questions found in this test.', 'warn');
        return;
      }
      const meta = extractTestMeta();
      if (seriesName) meta.test_title = `[TestRanking][MOCK] ${seriesName}`.slice(0, 200);
      meta.test_url = window.location.origin + window.location.pathname; // strip any stale #tbel-q
      await sendToServer({ meta, questions }, questions.length);
    } catch (err) {
      toast(`❌ ${err.message}`, 'error');
    }
  }

  // ---------- TestRanking deep-link jump ----------
  // Flashcards store source_url like <solution page>#tbel-q=7[&tbel-sn=PART-A].
  // The page never encodes the question in its URL, but clicking a palette cell
  // (div.test_right_panel_icon_pt, text = question number) switches the question,
  // so we consume our own hash and click the matching cell once the SPA hydrates.

  function trParseHash() {
    const h = window.location.hash.replace(/^#/, '');
    if (!/(^|&)tbel-q=/.test(h)) return null;
    const p = new URLSearchParams(h);
    const q = parseInt(p.get('tbel-q'), 10);
    return q > 0 ? { q, sectionName: p.get('tbel-sn') || null } : null;
  }

  // Best-effort section tab click for multi-section tests: find a small clickable
  // element whose trimmed text equals the section name.
  async function trClickSectionTab(name) {
    const cand = Array.from(document.querySelectorAll('div, span, li, button, a')).find(
      (el) =>
        el.children.length <= 1 &&
        (el.textContent || '').trim().toLowerCase() === name.trim().toLowerCase()
    );
    if (cand) {
      cand.click();
      await sleep(400);
    }
  }

  async function trJumpToQuestion() {
    const target = trParseHash();
    if (!target || !trTestId()) return;
    for (let tries = 0; tries < 60; tries++) { // up to ~15s for Next.js hydration
      let cells = Array.from(document.querySelectorAll('div.test_right_panel_icon_pt'));
      if (cells.length) {
        if (target.sectionName) {
          await trClickSectionTab(target.sectionName);
          cells = Array.from(document.querySelectorAll('div.test_right_panel_icon_pt'));
        }
        const cell = cells.find((c) => (c.textContent || '').trim() === String(target.q));
        if (cell) {
          cell.click();
          toast(`Jumped to question ${target.q}`, 'info');
          return;
        }
      }
      await sleep(250);
    }
    toast(`Could not find question ${target.q} in the palette.`, 'warn');
  }

  // ---------- Actions ----------
  async function handleScan() {
    if (PLATFORM === 'oliveboard') return handleScanOliveboard();
    if (PLATFORM === 'testranking') return handleScanTestranking();
    toast('Scanning page for wrong answers...', 'info');
    const cards = extractAllQuestions();

    if (cards.length === 0) {
      toast('No question cards found. Try "Log current question" instead.', 'warn');
      return;
    }

    const questions = cards
      .map(extractQuestionData)
      .filter((q) => q.status === 'wrong' || q.status === 'skipped');

    if (questions.length === 0) {
      toast(`Found ${cards.length} questions but none marked wrong/skipped.`, 'warn');
      return;
    }

    const payload = {
      meta: extractTestMeta(),
      questions,
    };

    await sendToServer(payload, questions.length);
  }

  async function handleCurrent() {
    if (PLATFORM === 'testranking') {
      // No reliable way to detect the currently-displayed question number, and the
      // API scan is instant anyway — it captures every wrong & skipped question.
      toast('On TestRanking use "Scan page" — it captures all wrong & skipped questions at once.', 'warn');
      return;
    }
    if (PLATFORM === 'oliveboard') {
      const block = getVisibleOliveBlock();
      if (!block) {
        toast('No question is open. Click a question in the Solutions view first.', 'warn');
        return;
      }
      const q = extractOliveboardBlock(block, null);
      if (q.status === 'unknown' || q.status === 'correct') q.status = 'wrong'; // user explicitly logged it
      await sendToServer({ meta: extractTestMeta(), questions: [q] }, 1);
      return;
    }
    // Just grab the whole visible question area
    const cards = extractAllQuestions();
    // Pick the one most visible in viewport
    let best = null;
    let bestScore = -1;
    cards.forEach((c) => {
      const r = c.getBoundingClientRect();
      const visible = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
      if (visible > bestScore) {
        bestScore = visible;
        best = c;
      }
    });

    if (!best) {
      // Try targeted fallbacks before grabbing the entire page.
      // Testbook single-question view: look for the question pane by common class patterns.
      const targeted = document.querySelector(
        '[class*="question-pane"], [class*="QuestionPane"], ' +
        '[class*="quiz-content"], [class*="QuizContent"], ' +
        '[class*="questionContent"], [class*="question-content"]'
      );
      best = targeted || document.querySelector('main') || document.body;
    }

    const q = extractQuestionData(best);
    q.status = 'wrong'; // user explicitly chose to log this

    const payload = {
      meta: extractTestMeta(),
      questions: [q],
    };

    await sendToServer(payload, 1);
  }

  async function sendToServer(payload, count) {
    try {
      toast(`Sending ${count} question(s) to server...`, 'info');
      const data = await serverFetch('/api/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast(`✅ Saved ${data.saved} mistake(s). AI categorizing in background...`, 'success');
      updateCount();
    } catch (err) {
      const hint = /reload|disconnected/i.test(err.message) ? '' : ' Is the server running?';
      toast(`❌ ${err.message}.${hint}`, 'error');
    }
  }

  async function updateCount() {
    try {
      const data = await serverFetch('/api/stats');
      const el = document.getElementById('tb-el-count');
      if (el && data.pending_review) {
        el.textContent = data.pending_review;
        el.style.display = 'inline-flex';
      }
    } catch (e) {
      // server offline, silent
    }
  }

  // ---------- Init ----------
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectButton);
  } else {
    injectButton();
  }
  setTimeout(updateCount, 1500);

  if (PLATFORM === 'testranking') {
    trJumpToQuestion(); // consume #tbel-q= deep links from the dashboard
    window.addEventListener('hashchange', trJumpToQuestion);
  }
})();
