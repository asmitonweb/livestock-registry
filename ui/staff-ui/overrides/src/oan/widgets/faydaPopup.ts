/** Collect FIN in the same popup that will navigate to eSignet. */
export function collectFaydaFin(
    popup: Window,
    initial: string,
    prompt: string,
    timeoutMs = 5 * 60 * 1000,
): Promise<string | null> {
    const doc = popup.document;
    doc.title = 'Fayda verification';
    doc.body.replaceChildren();
    doc.body.style.cssText = 'font-family:system-ui,sans-serif;padding:32px;max-width:440px;margin:auto';
    const title = doc.createElement('h2');
    title.textContent = 'Fayda verification';
    const help = doc.createElement('p');
    help.textContent = prompt;
    const form = doc.createElement('form');
    const label = doc.createElement('label');
    label.htmlFor = 'fayda-fin';
    label.textContent = 'Fayda ID (FIN)';
    const input = doc.createElement('input');
    input.id = 'fayda-fin';
    input.name = 'fin';
    input.inputMode = 'numeric';
    input.pattern = '[0-9]{12}';
    input.maxLength = 12;
    input.required = true;
    input.autocomplete = 'off';
    input.value = initial;
    input.style.cssText = 'display:block;width:100%;box-sizing:border-box;padding:10px;margin:8px 0 16px';
    const button = doc.createElement('button');
    button.type = 'submit';
    button.textContent = 'Continue';
    const error = doc.createElement('p');
    error.setAttribute('role', 'alert');
    form.append(label, input, button, error);
    doc.body.append(title, help, form);
    input.focus();
    return new Promise((resolve) => {
        let finished = false;
        const finish = (value: string | null) => {
            if (finished) return;
            finished = true;
            window.clearInterval(closed);
            window.clearTimeout(expired);
            form.removeEventListener('submit', submit);
            resolve(value);
        };
        const submit = (event: Event) => {
            event.preventDefault();
            const value = input.value.trim();
            if (!/^[0-9]{12}$/.test(value)) {
                error.textContent = 'FIN (Fayda ID): enter all 12 digits to continue.';
                return;
            }
            button.disabled = true;
            finish(value); // Keep this window open for the authorization redirect.
        };
        const closed = window.setInterval(() => {
            if (popup.closed) finish(null);
        }, 200);
        const expired = window.setTimeout(() => finish(null), timeoutMs);
        form.addEventListener('submit', submit);
    });
}
