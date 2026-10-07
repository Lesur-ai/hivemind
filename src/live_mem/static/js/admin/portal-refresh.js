/* One active Portal view; consumers own their reads and guard painting with isCurrent. */
const PortalRefresh = (() => {
    const KEY = 'hivemind.portal.autoRefresh';
    const defaults = () => ({ enabled: false, intervalSeconds: 15 });
    function validate(value) {
        return value && typeof value === 'object' && !Array.isArray(value)
            && Object.keys(value).length === 2
            && Object.hasOwn(value, 'enabled') && Object.hasOwn(value, 'intervalSeconds')
            && typeof value.enabled === 'boolean' && [15, 30, 60].includes(value.intervalSeconds)
            ? { enabled: value.enabled, intervalSeconds: value.intervalSeconds } : defaults();
    }
    let preference;
    try { preference = validate(JSON.parse(localStorage.getItem(KEY))); }
    catch (_) { preference = defaults(); }
    let sessionActive = false;
    let current = null;
    let timer = null;
    let wasHidden = document.hidden;

    function owned(owner) {
        return !!owner && sessionActive && current === owner
            && AdminRouter.epoch === owner.epoch
            && sessionGenerationIsCurrent(owner.sessionGeneration);
    }
    function eligible(owner) {
        if (!owned(owner) || !owner.follow) return false;
        try { return owner.canAuto() === true; }
        catch (_) { return false; }
    }
    function state() {
        const available = owned(current);
        return {
            ...preference, available, eligible: eligible(current),
            busy: available && !!current.flight,
            error: available ? current.error : null,
            lastSuccess: available ? current.lastSuccess : null,
        };
    }
    function notify() { document.dispatchEvent(new CustomEvent('portal:refresh-change')); }
    function cancelTimer() {
        if (timer !== null) clearTimeout(timer);
        timer = null;
    }
    function schedule(owner) {
        cancelTimer();
        if (!preference.enabled || !eligible(owner) || document.hidden || owner.flight) return;
        const seconds = Math.min(preference.intervalSeconds * (2 ** Math.min(owner.failures, 4)), 120);
        timer = setTimeout(() => {
            timer = null;
            run(owner, true).catch(() => {});
        }, seconds * 1000);
    }
    function run(owner, automatic) {
        if (!owned(owner)) return Promise.resolve();
        if (document.hidden) {
            if (!automatic) owner.manualPending = true;
            return Promise.resolve();
        }
        if (automatic && (!preference.enabled || !eligible(owner))) return Promise.resolve();
        if (owner.flight) return owner.flight;
        owner.manualPending = false;
        cancelTimer();
        const automaticGeneration = owner.automaticGeneration;
        const isCurrent = () => owned(owner) && (!automatic
            || (preference.enabled && owner.automaticGeneration === automaticGeneration));
        owner.flight = Promise.resolve().then(() => {
            if (!isCurrent()) return;
            if (document.hidden) {
                if (!automatic) owner.manualPending = true;
                return { skipped: true };
            }
            return owner.refresh({ automatic, isCurrent });
        }).then(result => {
            if (!isCurrent() || result?.skipped === true) return result;
            owner.lastSuccess = Date.now();
            owner.error = null;
            owner.failures = 0;
            owner.follow = result?.follow !== false;
            return result;
        }, error => {
            if (isCurrent()) {
                // Keep transport payloads and potentially sensitive messages out of shared UI state.
                owner.error = 'Refresh failed';
                owner.failures += 1;
            }
            throw error;
        }).finally(() => {
            owner.flight = null;
            // Re-enabling during a cancelled read requests a new cycle after it settles.
            if (owned(owner) && owner.manualPending && !document.hidden) run(owner, false).catch(() => {});
            else if (isCurrent() || (owned(owner) && owner.restartRequested)) schedule(owner);
            owner.restartRequested = false;
            if (owned(owner)) notify();
        });
        notify();
        return owner.flight;
    }
    function clearRoute() {
        cancelTimer();
        current = null;
        notify();
    }
    function configure(value) {
        preference = validate(value);
        try { localStorage.setItem(KEY, JSON.stringify(preference)); }
        catch (_) { preference = defaults(); }
        if (owned(current)) {
            if (!preference.enabled) current.automaticGeneration += 1;
            current.restartRequested = preference.enabled && !!current.flight;
        }
        schedule(current);
        notify();
    }
    function register({ refresh, canAuto = () => true }, ctx) {
        cancelTimer();
        current = {
            refresh, canAuto, epoch: ctx.epoch, sessionGeneration: ctx.sessionGeneration,
            follow: true, flight: null, failures: 0, error: null, lastSuccess: null,
            automaticGeneration: 0, restartRequested: false, manualPending: false,
        };
        schedule(current);
        notify();
    }
    function beginSession() {
        clearRoute();
        sessionActive = true;
    }
    function endSession({ logout = false } = {}) {
        sessionActive = false;
        clearRoute();
        if (logout) configure({ ...preference, enabled: false });
    }
    document.addEventListener('visibilitychange', () => {
        const returning = wasHidden && !document.hidden;
        wasHidden = document.hidden;
        if (document.hidden) cancelTimer();
        else if (returning && owned(current) && current.manualPending) run(current, false).catch(() => {});
        else if (returning && preference.enabled && eligible(current)) run(current, true).catch(() => {});
    });
    return { register, refresh: () => run(current, false), configure, beginSession, endSession, clearRoute, state };
})();
