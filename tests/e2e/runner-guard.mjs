export const ACTIVE_RUNNER_ENV = 'HIVEMIND_ACTIVE_TEST_RUNNER';

/**
 * Claim the dedicated Playwright runner without rejecting its own workers.
 *
 * Playwright reloads its configuration in direct worker children. A second
 * Playwright command started by a test is instead a child of that worker, so
 * its parent PID cannot match the PID that originally claimed the runner.
 */
export function claimPlaywrightRunner({
    environment = process.env,
    pid = process.pid,
    parentPid = process.ppid,
} = {}) {
    const activeRunner = (environment[ACTIVE_RUNNER_ENV] || '').trim();
    if (!activeRunner) {
        const identity = `playwright:${pid}`;
        environment[ACTIVE_RUNNER_ENV] = identity;
        return identity;
    }

    const match = /^playwright:([1-9][0-9]*)$/.exec(activeRunner);
    const ownerPid = match ? Number.parseInt(match[1], 10) : undefined;
    const isOwnerReload = ownerPid === pid;
    const isDirectWorker =
        ownerPid === parentPid && environment.TEST_WORKER_INDEX !== undefined;
    if (isOwnerReload || isDirectWorker) {
        return activeRunner;
    }

    throw new Error(
        `refusing nested Playwright suite; active runner is ${JSON.stringify(activeRunner)}`,
    );
}
