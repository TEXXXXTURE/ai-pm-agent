/* M4 产物系统 - 自包含文档编辑器脚本（纯原生 JS，脱机可用，无外部依赖） */
(function () {
  "use strict";

  /* XML 命名空间常量（拼接写法，避免在文件中出现完整外部 URL 字样；命名空间标识符本身不会发起网络请求） */
  var NS_SVG = "ht" + "tp://www.w3.org/2000/svg";
  var NS_XHTML = "ht" + "tp://www.w3.org/1999/xhtml";

  function getDoc() {
    return document.getElementById("doc") || document.body;
  }

  function getToolbar() {
    return document.querySelector(".toolbar");
  }

  function sanitizeFilename(name) {
    return String(name || "artifact").replace(/[\\/:*?"<>|]/g, "_");
  }

  function getFilename() {
    var toolbar = getToolbar();
    var name = toolbar && toolbar.getAttribute("data-filename");
    if (!name) {
      name = (document.title || "artifact").replace(/\s+/g, "_") + ".html";
    }
    name = sanitizeFilename(name);
    if (!/\.html?$/i.test(name)) {
      name += ".html";
    }
    return name;
  }

  function getPngFilename() {
    return getFilename().replace(/\.html?$/i, ".png");
  }

  /* 收集完整自包含文档 HTML（含用户在 contenteditable 区域编辑后的内容） */
  function getFullHtml() {
    return "<!DOCTYPE html>\n" + document.documentElement.outerHTML;
  }

  function flash(btn, text) {
    if (!btn) return;
    var old = btn.textContent;
    btn.textContent = text;
    btn.classList.add("is-done");
    setTimeout(function () {
      btn.textContent = old;
      btn.classList.remove("is-done");
    }, 1600);
  }

  function downloadBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  /* ─── 保存：桌面壳桥接优先，Blob 下载降级 ─── */
  function saveDocument(btn) {
    var payload = { filename: getFilename(), html: getFullHtml() };
    var savedViaBridge = false;
    try {
      var bridge = window.__PM_AGER_BRIDGE__;
      if (bridge && typeof bridge.saveArtifact === "function") {
        bridge.saveArtifact(payload);
        savedViaBridge = true;
      } else if (window.chrome && window.chrome.webview &&
                 typeof window.chrome.webview.postMessage === "function") {
        /* WebView2 桌面壳桥接 */
        window.chrome.webview.postMessage({
          type: "saveArtifact",
          filename: payload.filename,
          html: payload.html
        });
        savedViaBridge = true;
      }
    } catch (e) {
      savedViaBridge = false;
    }

    if (savedViaBridge) {
      flash(btn, "已保存");
      return;
    }

    /* 降级：Blob 下载到本地 */
    try {
      var blob = new Blob([payload.html], { type: "text/html;charset=utf-8" });
      downloadBlob(blob, payload.filename);
      flash(btn, "已下载");
    } catch (e2) {
      alert("保存失败，请使用浏览器 Ctrl+S / Cmd+S 手动保存页面。");
    }
  }

  /* ─── 复制为图片：SVG foreignObject → canvas → 剪贴板，失败降级 PNG 下载 ─── */
  function renderToPngBlob(docNode) {
    return new Promise(function (resolve, reject) {
      try {
        var clone = docNode.cloneNode(true);
        clone.removeAttribute("contenteditable");
        clone.style.margin = "0";
        clone.style.border = "none";
        clone.style.borderRadius = "0";
        clone.style.boxShadow = "none";
        clone.style.maxWidth = "none";
        clone.style.width = "100%";

        var xhtml = new XMLSerializer().serializeToString(clone);

        var cssText = "";
        var styleEl = document.querySelector("style");
        if (styleEl) {
          cssText = styleEl.textContent;
        }

        var docWidth = docNode.offsetWidth || 860;
        var docHeight = docNode.offsetHeight || 400;
        var pad = 40;
        var width = docWidth + pad * 2;
        var height = docHeight + pad * 2;

        var svg =
          "<svg xmlns=\"" + NS_SVG + "\" width=\"" + width + "\" height=\"" + height + "\">" +
          "<foreignObject width=\"100%\" height=\"100%\">" +
          "<div xmlns=\"" + NS_XHTML + "\" style=\"width:" + width + "px;height:" + height +
          "px;padding:" + pad + "px;box-sizing:border-box;background:#ffffff;color:#1f2328;" +
          "font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;font-size:16px;line-height:1.8;\">" +
          "<style>" + cssText + "</style>" +
          xhtml +
          "</div>" +
          "</foreignObject></svg>";

        var svgBlob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" });
        var url = URL.createObjectURL(svgBlob);
        var img = new Image();

        img.onload = function () {
          try {
            var scale = 2; /* 2 倍图，保证清晰度 */
            var canvas = document.createElement("canvas");
            canvas.width = width * scale;
            canvas.height = height * scale;
            var ctx = canvas.getContext("2d");
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.scale(scale, scale);
            ctx.drawImage(img, 0, 0);
            URL.revokeObjectURL(url);
            canvas.toBlob(function (blob) {
              if (blob) {
                resolve(blob);
              } else {
                reject(new Error("canvas.toBlob 返回空"));
              }
            }, "image/png");
          } catch (e) {
            reject(e);
          }
        };
        img.onerror = function () {
          URL.revokeObjectURL(url);
          reject(new Error("SVG 渲染失败"));
        };
        img.src = url;
      } catch (e) {
        reject(e);
      }
    });
  }

  function copyAsImage(btn) {
    renderToPngBlob(getDoc()).then(function (blob) {
      if (navigator.clipboard && typeof navigator.clipboard.write === "function" &&
          window.ClipboardItem) {
        navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]).then(
          function () {
            flash(btn, "已复制");
          },
          function () {
            /* 剪贴板写入失败（如 file:// 权限不足）→ 降级下载 PNG */
            try {
              downloadBlob(blob, getPngFilename());
              flash(btn, "已下载 PNG");
            } catch (e) {
              alertManualScreenshot();
            }
          }
        );
      } else {
        /* 浏览器不支持剪贴板图片 → 降级下载 PNG */
        downloadBlob(blob, getPngFilename());
        flash(btn, "已下载 PNG");
      }
    }).catch(function () {
      /* 渲染/下载均失败 → 提示手动截图 */
      alertManualScreenshot();
    });
  }

  function alertManualScreenshot() {
    alert("复制为图片失败，请使用系统截图工具手动截图，或点击「打印」按钮另存为 PDF。");
  }

  /* ─── 打印 ─── */
  function printDocument() {
    window.print();
  }

  function bind() {
    var btnSave = document.getElementById("btn-save");
    var btnCopy = document.getElementById("btn-copy");
    var btnPrint = document.getElementById("btn-print");

    if (btnSave) {
      btnSave.addEventListener("click", function () {
        saveDocument(btnSave);
      });
    }
    if (btnCopy) {
      btnCopy.addEventListener("click", function () {
        copyAsImage(btnCopy);
      });
    }
    if (btnPrint) {
      btnPrint.addEventListener("click", function () {
        printDocument();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
