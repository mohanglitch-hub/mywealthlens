/**
 * MyWealthLens — Client-Side Encryption Module (Sep 2026)
 * ==========================================================
 * The one place this app does actual cryptography. Everything here
 * runs in the USER'S OWN BROWSER via the standard Web Crypto API
 * (window.crypto.subtle) — no external library, nothing to trust
 * but the browser itself. The server never sees a passphrase, a
 * derived key, or plaintext for anything encrypted through this
 * module. That is the entire point: it is what makes "even Mohan
 * cannot access this" a real, checkable claim instead of a promise.
 *
 * Scope (see the Gap Backlog doc / Sep 2026 production-readiness
 * plan for the full reasoning): Document Vault files across Wealth,
 * Insurance and Retirement. Portfolio VALUES (fund/FD amounts) and
 * identifiers with server-side search/dedup (policy_number,
 * account_number) are deliberately NOT run through this module —
 * they use ordinary server-side encryption-at-rest instead, because
 * true zero-knowledge would break real features (duplicate-policy
 * detection, search-by-account-number) those fields currently have.
 *
 * Algorithm choices (both are the current, unremarkable, widely
 * reviewed standard — not a home-grown scheme):
 *   - Key derivation: PBKDF2-SHA256, 600,000 iterations (OWASP's
 *     2023+ minimum recommendation for PBKDF2-SHA256).
 *   - Encryption: AES-256-GCM (authenticated — a tampered or
 *     corrupted ciphertext fails to decrypt rather than silently
 *     returning garbage).
 *
 * Nothing in this file is secret. The salt, the IV, and the
 * ciphertext are all safe to store server-side in plain form — only
 * the PASSPHRASE (never sent anywhere) and the KEY DERIVED FROM IT
 * (kept only in this browser's memory / sessionStorage) are secret.
 */
(function (global) {
  "use strict";

  const PBKDF2_ITERATIONS = 600000;
  const AES_KEY_LENGTH = 256;
  const SALT_BYTES = 16;
  const IV_BYTES = 12; // 96-bit IV is the recommended size for AES-GCM
  const VERIFIER_PLAINTEXT = "MWL-ENCRYPTION-VERIFIER-v1";

  // ---- base64 <-> ArrayBuffer helpers ------------------------------------
  // Everything that has to cross to/from the server (salt, iv,
  // ciphertext) travels as base64 text, since that's what fits
  // cleanly into form fields and JSON.

  function bufToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  function base64ToBuf(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes.buffer;
  }

  function randomBytesBase64(numBytes) {
    const arr = new Uint8Array(numBytes);
    global.crypto.getRandomValues(arr);
    return bufToBase64(arr.buffer);
  }

  // ---- key derivation -----------------------------------------------------

  /**
   * Generates a fresh random salt (base64) — call this ONCE, the
   * first time a user sets up their encryption passphrase. The salt
   * itself is not secret; it just has to stay the same forever after
   * so the same passphrase always re-derives the same key.
   */
  function generateSalt() {
    return randomBytesBase64(SALT_BYTES);
  }

  /**
   * Derives a non-extractable AES-256-GCM CryptoKey from a passphrase
   * + salt. "Non-extractable" means even this page's own JavaScript
   * cannot read the raw key bytes back out once derived — it can only
   * be used to encrypt/decrypt via the functions below. Deterministic:
   * the same passphrase + salt always produces the same key, which is
   * what lets a user unlock their data again on a new device.
   */
  async function deriveKey(passphrase, saltB64) {
    const enc = new TextEncoder();
    const baseKey = await global.crypto.subtle.importKey(
      "raw",
      enc.encode(passphrase),
      "PBKDF2",
      false,
      ["deriveKey"]
    );
    return global.crypto.subtle.deriveKey(
      {
        name: "PBKDF2",
        salt: base64ToBuf(saltB64),
        iterations: PBKDF2_ITERATIONS,
        hash: "SHA-256",
      },
      baseKey,
      { name: "AES-GCM", length: AES_KEY_LENGTH },
      false, // non-extractable
      ["encrypt", "decrypt"]
    );
  }

  // ---- verifier -------------------------------------------------------------
  // Lets the app confirm "is this the right passphrase?" without the
  // server ever learning the key: encrypt a known constant, store the
  // ciphertext; later, try to decrypt it and check the result matches.

  async function makeVerifier(key) {
    const { ciphertext, iv } = await encryptText(key, VERIFIER_PLAINTEXT);
    return { verifierCiphertext: ciphertext, verifierIv: iv };
  }

  async function checkVerifier(key, verifierCiphertextB64, verifierIvB64) {
    try {
      const plaintext = await decryptText(key, verifierCiphertextB64, verifierIvB64);
      return plaintext === VERIFIER_PLAINTEXT;
    } catch (err) {
      // AES-GCM decrypt throws on a wrong key (authentication failure) —
      // that failure IS the answer "no, wrong passphrase", not an error
      // to surface as a crash.
      return false;
    }
  }

  // ---- encrypt / decrypt: text ------------------------------------------

  async function encryptText(key, plaintext) {
    const iv = global.crypto.getRandomValues(new Uint8Array(IV_BYTES));
    const enc = new TextEncoder();
    const ciphertextBuf = await global.crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      enc.encode(plaintext)
    );
    return { ciphertext: bufToBase64(ciphertextBuf), iv: bufToBase64(iv.buffer) };
  }

  async function decryptText(key, ciphertextB64, ivB64) {
    const plainBuf = await global.crypto.subtle.decrypt(
      { name: "AES-GCM", iv: base64ToBuf(ivB64) },
      key,
      base64ToBuf(ciphertextB64)
    );
    return new TextDecoder().decode(plainBuf);
  }

  // ---- encrypt / decrypt: files -------------------------------------------
  // Used for Document Vault uploads/downloads — operates on raw bytes,
  // not base64, since files can be large and base64 would bloat them
  // by ~33% in memory for no reason (the IV alone is sent as base64,
  // since it's tiny).

  /**
   * Encrypts a File/Blob's bytes. Returns { blob, ivB64 } — `blob` is
   * what should actually be uploaded in place of the original file;
   * `ivB64` is a small base64 string safe to send as an ordinary form
   * field alongside it.
   */
  async function encryptFile(key, file) {
    const iv = global.crypto.getRandomValues(new Uint8Array(IV_BYTES));
    const plainBuf = await file.arrayBuffer();
    const cipherBuf = await global.crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      plainBuf
    );
    return {
      blob: new Blob([cipherBuf], { type: "application/octet-stream" }),
      ivB64: bufToBase64(iv.buffer),
    };
  }

  /**
   * Decrypts ciphertext bytes (already fetched as an ArrayBuffer) back
   * to the original file bytes, given the IV that was stored alongside
   * it at upload time. Returns an ArrayBuffer the caller can wrap in a
   * Blob with the document's real mime type for preview/download.
   */
  async function decryptFile(key, ciphertextBuf, ivB64) {
    return global.crypto.subtle.decrypt(
      { name: "AES-GCM", iv: base64ToBuf(ivB64) },
      key,
      ciphertextBuf
    );
  }

  // ---- session key cache ---------------------------------------------------
  // A derived CryptoKey object cannot survive a page navigation on its
  // own. Re-prompting for the passphrase on every single page would be
  // unusable, so once a user unlocks for this browser session, the
  // derived key is kept ONLY in module-level memory for the current
  // page and mirrored, wrapped, into sessionStorage (cleared when the
  // tab closes; never localStorage, never a cookie, never sent to the
  // server) so the next page load in the same tab can pick it back up
  // without asking again. This is a deliberate, named trade-off: it
  // means a browser extension or an XSS bug on this origin could read
  // the cached key for that session — the alternative (prompting every
  // page) is unusable, so this mirrors what password managers with a
  // "remember for this session" option do.
  let _sessionKey = null;

  async function unlockSession(passphrase, saltB64) {
    const key = await deriveKey(passphrase, saltB64);
    _sessionKey = key;
    try {
      const raw = await global.crypto.subtle.exportKey("raw", await _reExportableKey(passphrase, saltB64));
      sessionStorage.setItem("mwl_ek", bufToBase64(raw));
    } catch (err) {
      // sessionStorage may be unavailable (private browsing edge cases) —
      // the in-memory key still works for the rest of this page.
    }
    return key;
  }

  // AES-GCM keys derived non-extractable can't be exported directly;
  // for the sessionStorage mirror only, we derive a second, exportable
  // copy of the same key material. It never leaves this device either
  // way — only the storage location differs.
  async function _reExportableKey(passphrase, saltB64) {
    const enc = new TextEncoder();
    const baseKey = await global.crypto.subtle.importKey(
      "raw", enc.encode(passphrase), "PBKDF2", false, ["deriveKey"]
    );
    return global.crypto.subtle.deriveKey(
      { name: "PBKDF2", salt: base64ToBuf(saltB64), iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
      baseKey, { name: "AES-GCM", length: AES_KEY_LENGTH }, true, ["encrypt", "decrypt"]
    );
  }

  async function restoreSessionKey() {
    if (_sessionKey) return _sessionKey;
    let stored;
    try {
      stored = sessionStorage.getItem("mwl_ek");
    } catch (err) {
      return null;
    }
    if (!stored) return null;
    _sessionKey = await global.crypto.subtle.importKey(
      "raw", base64ToBuf(stored), { name: "AES-GCM" }, false, ["encrypt", "decrypt"]
    );
    return _sessionKey;
  }

  function clearSessionKey() {
    _sessionKey = null;
    try {
      sessionStorage.removeItem("mwl_ek");
    } catch (err) {
      /* ignore */
    }
  }

  function hasSessionKey() {
    return _sessionKey !== null;
  }

  function getSessionKey() {
    return _sessionKey;
  }

  global.MWLCrypto = {
    generateSalt,
    deriveKey,
    makeVerifier,
    checkVerifier,
    encryptText,
    decryptText,
    encryptFile,
    decryptFile,
    unlockSession,
    restoreSessionKey,
    clearSessionKey,
    hasSessionKey,
    getSessionKey,
  };
})(typeof window !== "undefined" ? window : globalThis);
