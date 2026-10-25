'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { collectFaydaFin } from './faydaPopup';
import {
    getValueByPath,
    setValueByPath,
    setValues,
    useWidgetContext,
    type WidgetRootState,
} from '@openg2p/registry-widgets';

type Config = Record<string, any>;
type Result = {
    verification_id: string;
    status: 'PENDING' | 'SUCCESS' | 'FAILURE' | 'EXPIRED' | string;
    message?: string | null;
    verified_at?: string | null;
    expires_at?: string | null;
    foundational_id?: string | null;
    values?: Record<string, unknown>;
};

const TERMINAL = new Set(['SUCCESS', 'FAILURE', 'EXPIRED']);
const POLL_MS = 1500;
// Core keeps a sign-in open for five minutes (registrant_auth_session_ttl_seconds).
const GIVE_UP_MS = 6 * 60 * 1000;

const LABELS: Record<string, string> = {
    oan_idv_verify: 'Verify with Fayda',
    oan_idv_verify_again: 'Verify again',
    oan_idv_waiting: 'Waiting for the Fayda sign-in…',
    oan_idv_not_verified: 'Not verified',
    oan_idv_verified: 'Verified on {date}',
    oan_idv_failed: 'Not verified',
    oan_idv_filled: 'Filled in from Fayda: {fields}. These cannot be changed.',
    oan_idv_enter_fin: "Enter the farmer's Fayda ID (FIN) in this window to continue.",
    oan_idv_popup_blocked: 'The browser blocked the Fayda window. Allow pop-ups for this site and verify again.',
    oan_idv_closed: 'The Fayda window was closed before the sign-in finished. Verify again.',
    oan_idv_fin_changed: 'The FIN was changed after verification. Verify again with the new FIN.',
};

function popupFeatures(width: number, height: number): string {
    const w = Math.min(width, Math.floor(window.innerWidth * 0.92));
    const h = Math.min(height, Math.floor(window.innerHeight * 0.92));
    const left = Math.max(0, Math.floor(window.screenX + (window.innerWidth - w) / 2));
    const top = Math.max(0, Math.floor(window.screenY + (window.innerHeight - h) / 2));
    // No "noopener": with it window.open returns null, and the widget could not
    // tell when the window closes. The opener is cut by hand below instead.
    return `popup=yes,width=${w},height=${h},left=${left},top=${top},scrollbars=yes,resizable=yes`;
}

/**
 * Verifies the farmer with Fayda (eSignet) while the form is being filled in,
 * and fills in what Fayda verified.
 *
     * The FIN is collected in this verification window; this widget starts
     * core's registrant authentication for it (via /oan/identity-verification),
     * opens the Fayda sign-in in a pop-up and waits for the outcome. On success it puts the
 * verification and the verified values (names, gender, birth date, as the
 * provider's claim mapping gives them) on the form, so they are saved with
 * their sections. The server checks the verification on every save and
 * refuses a change to a verified value. Test identities are chosen in the
 * Fayda pop-up (mock identity system), never on this widget.
 *
 * Section metadata:
 *   widget: "oan-identity-verification"
 *   widget-data-path: {verificationId, status, verifiedOn, expiresOn}  (data paths)
 *   widget-data-options: {registerId, foundationalIdPath, internalRecordIdPath,
 *                         fieldPrefix (the register's data path prefix),
 *                         popupWidth, popupHeight}
 */
export function IdentityVerificationWidget({ config }: { config: Config }) {
    const dispatch = useDispatch();
    const { t, schemaData, dataSourceRequestHandler } = useWidgetContext();
    const values = useSelector((state: WidgetRootState) => state.widget.values);
    const paths = (config['widget-data-path'] ?? {}) as Record<string, string>;
    const options = (config['widget-data-options'] ?? {}) as Record<string, any>;
    const readonly = config['widget-readonly'] === true || config['widget-disabled'] === true;

    const read = useCallback(
        (path?: string) => {
            if (!path) return undefined;
            const typed = getValueByPath(values, path);
            return typed !== undefined ? typed : getValueByPath(schemaData ?? {}, path);
        },
        [values, schemaData],
    );
    const say = useCallback(
        (key: string, params: Record<string, string> = {}) => {
            const fallback = LABELS[key] ?? key;
            const text = t ? t(key, { ...params, defaultValue: fallback }) : fallback;
            const template = !text || text === key ? fallback : text;
            return Object.entries(params).reduce((s, [k, v]) => s.replace(`{${k}}`, v), template);
        },
        [t],
    );
    const fieldLabel = useCallback(
        (field: string) => {
            const text = t ? t(field, { defaultValue: field }) : field;
            return text || field;
        },
        [t],
    );

    const fin = String(read(options.foundationalIdPath) ?? '').trim();
    const savedStatus = read(paths.status) as string | undefined;
    const savedOn = read(paths.verifiedOn) as string | undefined;

    const [busy, setBusy] = useState(false);
    const [problem, setProblem] = useState<string | null>(null);
    const [filled, setFilled] = useState<string[]>([]);
    const [verifiedFin, setVerifiedFin] = useState<string | null>(null);
    const popup = useRef<Window | null>(null);
    const timer = useRef<number | null>(null);

    const stop = useCallback(() => {
        if (timer.current) window.clearTimeout(timer.current);
        timer.current = null;
        setBusy(false);
    }, []);
    // A message about the FIN, or about a sign-in for another FIN, no longer
    // applies once the FIN changes.
    useEffect(() => setProblem(null), [fin]);
    useEffect(() => () => {
        if (timer.current) window.clearTimeout(timer.current);
        try { popup.current?.close(); } catch { /* the window may be gone */ }
    }, []);

    const apply = useCallback(
        (result: Result) => {
            const prefix = String(options.fieldPrefix ?? '');
            let patch: Record<string, unknown> = {};
            const put = (path: string | undefined, value: unknown) => {
                if (path) patch = setValueByPath(patch, path, value);
            };
            put(paths.verificationId, result.verification_id);
            put(paths.status, 'SUCCESS');
            put(paths.verifiedOn, result.verified_at?.slice(0, 10) ?? null);
            put(paths.expiresOn, result.expires_at?.slice(0, 10) ?? null);
            if (result.foundational_id) put(options.foundationalIdPath, result.foundational_id);
            const fields = Object.keys(result.values ?? {});
            for (const field of fields) put(prefix ? `${prefix}.${field}` : field, result.values![field]);
            dispatch(setValues(patch as never));
            setFilled(fields);
            setVerifiedFin(result.foundational_id ?? fin);
        },
        [dispatch, fin, options.fieldPrefix, options.foundationalIdPath, paths],
    );

    const poll = useCallback(
        (verificationId: string, startedAt: number, lookedAfterClose = false) => {
            timer.current = window.setTimeout(async () => {
                // Read before asking: core completes the sign-in when Fayda returns
                // to it, just before that page closes, so the answer after a close
                // is final.
                const closed = !popup.current || popup.current.closed;
                let result: Result | null;
                try {
                    result = await dataSourceRequestHandler!(
                        'oan', 'identity-verification/result', 'POST', { verification_id: verificationId },
                    );
                } catch {
                    stop();
                    setProblem(say('oan_idv_closed'));
                    try { popup.current?.close(); } catch { /* already closed */ }
                    return;
                }
                if (result && TERMINAL.has(result.status)) {
                    stop();
                    try { popup.current?.close(); } catch { /* already closed */ }
                    if (result.status === 'SUCCESS') apply(result);
                    else setProblem(result.message ?? say('oan_idv_closed'));
                    return;
                }
                if (!result) {
                    stop(); // the request handler has shown why
                    return;
                }
                if (lookedAfterClose || Date.now() - startedAt > GIVE_UP_MS) {
                    stop();
                    setProblem(say('oan_idv_closed'));
                    return;
                }
                poll(verificationId, startedAt, closed);
            }, POLL_MS);
        },
        [apply, dataSourceRequestHandler, say, stop],
    );

    const verify = useCallback(async () => {
        setProblem(null);
        // Open the window in the click, before the request, or it is blocked.
        const opened = window.open('', `${config['widget-id']}-fayda`, popupFeatures(
            Number(options.popupWidth ?? 1024), Number(options.popupHeight ?? 800)));
        if (!opened) {
            setProblem(say('oan_idv_popup_blocked'));
            return;
        }
        popup.current = opened;

        setBusy(true);
        let verificationFin: string | null;
        try {
            verificationFin = await collectFaydaFin(opened, fin, say('oan_idv_enter_fin'));
        } catch {
            opened.close();
            popup.current = null;
            stop();
            setProblem(say('oan_idv_closed'));
            return;
        }
        if (!verificationFin) {
            opened.close();
            popup.current = null;
            stop();
            setProblem(say('oan_idv_closed'));
            return;
        }
        opened.opener = null;
        setBusy(true);
        let started;
        try {
            started = await dataSourceRequestHandler!('oan', 'identity-verification/start', 'POST', {
            register_id: options.registerId,
            foundational_id: verificationFin,
            internal_record_id: read(options.internalRecordIdPath) ?? null,
            });
        } catch {
            opened.close();
            popup.current = null;
            stop();
            setProblem(say('oan_idv_closed'));
            return;
        }
        if (!started?.authorization_url) {
            // The request handler has shown the server's message.
            opened.close();
            popup.current = null;
            setBusy(false);
            return;
        }
        if (opened.closed) {
            stop();
            setProblem(say('oan_idv_closed'));
            return;
        }
        opened.location.href = started.authorization_url;
        poll(started.verification_id, Date.now());
    }, [config, dataSourceRequestHandler, fin, options, poll, read, say, stop]);

    const verified = savedStatus === 'SUCCESS';
    const finChanged = verified && verifiedFin !== null && verifiedFin !== fin;
    const label = String(config['widget-label'] ?? '');
    const shownLabel = t ? t(label, { defaultValue: label }) : label;
    const status = busy
        ? say('oan_idv_waiting')
        : verified
          ? say('oan_idv_verified', { date: savedOn ?? '' })
          : say(problem ? 'oan_idv_failed' : 'oan_idv_not_verified');
    const tone = busy ? 'var(--owt-color-warning)' : verified && !finChanged
        ? 'var(--owt-color-success)'
        : problem ? 'var(--owt-color-danger)' : 'var(--owt-color-text-muted)';

    return (
        <div className="mb-[10px]">
            <div className="flex flex-col sm:flex-row sm:items-start">
                <label
                    className="flex items-baseline min-w-0 max-w-full owt-field-label text-base font-medium md:min-w-[120px] sm:pr-4 sm:pt-1 mb-1 sm:mb-0"
                    style={{ fontFamily: 'Roboto, sans-serif' }}
                >
                    <span className="min-w-0 truncate">{shownLabel}</span>
                </label>
                <div className="flex-1 min-w-0 flex flex-col gap-1">
                    <div className="flex flex-wrap items-center gap-3">
                        <span
                            role="status"
                            className="inline-flex items-center gap-2 text-sm font-medium"
                            style={{ color: 'var(--owt-color-text)' }}
                        >
                            <span aria-hidden className="inline-block w-2 h-2 rounded-full" style={{ background: tone }} />
                            {status}
                        </span>
                        {!readonly && (
                            <button
                                type="button"
                                className="owt-btn owt-btn-primary text-sm px-4 py-1.5 rounded-[var(--owt-btn-border-radius)] disabled:opacity-50"
                                style={{
                                    background: 'var(--owt-color-primary)',
                                    color: 'var(--owt-color-bg)',
                                    border: '1px solid var(--owt-btn-primary-border)',
                                }}
                                onClick={verify}
                                // Enabled without a FIN too: a click then says to enter it,
                                // where a disabled button would do nothing and say nothing.
                                disabled={busy}
                            >
                                {say(verified ? 'oan_idv_verify_again' : 'oan_idv_verify')}
                            </button>
                        )}
                    </div>
                    {finChanged && <span className="text-xs" style={{ color: 'var(--owt-widget-error-color)' }}>{say('oan_idv_fin_changed')}</span>}
                    {problem && !busy && <span className="text-xs" style={{ color: 'var(--owt-widget-error-color)' }}>{problem}</span>}
                    {verified && filled.length > 0 && (
                        <span className="text-xs" style={{ color: 'var(--owt-color-text-muted)' }}>
                            {say('oan_idv_filled', { fields: filled.map(fieldLabel).join(', ') })}
                        </span>
                    )}
                </div>
            </div>
        </div>
    );
}
