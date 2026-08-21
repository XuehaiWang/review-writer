// P0 intentionally keeps browser polling simple and low-frequency. Jobs are
// durable in PostgreSQL, so the UI never needs one-second polling to preserve
// progress when the page is refreshed or another API instance serves it.
export const ACTIVE_JOB_POLL_INTERVAL_MS = 2_500;

export const PUBLICATION_POLL_INTERVAL_MS = 3_000;
