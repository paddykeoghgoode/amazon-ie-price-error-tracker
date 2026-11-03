/* Live GitHub Actions status for the public Amazon.ie tracker.
 * Uses unauthenticated GitHub REST GETs. No tokens or private workflow data
 * are embedded in the site. Percentages describe reported workflow steps,
 * never the share of Amazon products scanned.
 */
"use strict";

const SCAN_ACTIVITY_REPOSITORY = "paddykeoghgoode/amazon-ie-price-error-tracker";
const SCAN_ACTIVITY_API = "https://api.github.com/repos/" + SCAN_ACTIVITY_REPOSITORY;
const SCAN_ACTIVITY_POLL_MS = 150000;       // 24 run-list calls per hour
const SCAN_ACTIVITY_JOBS_MS = 240000;       // <= 15 calls per hour/active run
const SCAN_ACTIVITY_MAX_ACTIVE_DETAILS = 2; // <= ~54 calls/hour total
const SCAN_ACTIVITY_TRACKER_PATHS = new Set([
  ".github/workflows/pages.yml",
  ".github/workflows/hotlist.yml",
]);
const SCAN_ACTIVITY_TERMINAL = new Set(["completed"]);

function trackerWorkflowRun(run) {
  if (!run || typeof run !== "object") return false;
  return SCAN_ACTIVITY_TRACKER_PATHS.has(run.path) ||
    run.name === "Scan and publish tracker" ||
    run.name === "Fast checks for price-drop leads" ||
    run.name === "Fast checks for CeX targets and price drops" ||
    run.name === "Fast checks for confirmed drops";
}

function trackerWorkflowName(run) {
  if (run.path === ".github/workflows/hotlist.yml" ||
      String(run.name || "").startsWith("Fast checks")) {
    return "CeX targets & price-drop checks";
  }
  return run.event === "issues" ? "Watchlist change" : "Watchlist & bestseller tracker";
}

function trackerStatus(run) {
  const status = String(run.status || "");
  const conclusion = String(run.conclusion || "");
  if (status === "completed") {
    if (conclusion === "success") return { text: "Completed", css: "ok" };
    if (conclusion === "skipped") return { text: "Skipped", css: "queued" };
    if (conclusion === "cancelled") return { text: "Cancelled", css: "queued" };
    return { text: "Failed", css: "failed" };
  }
  if (status === "in_progress") return { text: "Running", css: "running" };
  if (status === "queued" || status === "requested" || status === "pending" ||
      status === "waiting") {
    return { text: "Queued", css: "queued" };
  }
  return { text: "Waiting", css: "queued" };
}

function trackerStepProgress(jobs, runStatus) {
  if (!Array.isArray(jobs) || jobs.length === 0) return null;
  // GitHub sometimes reports jobs without steps until the runner starts.
  // Count each as one pending slot; completed skipped jobs are ignored.
  const relevant = jobs.filter((job) => job && job.conclusion !== "skipped");
  if (!relevant.length) return null;
  let totalSteps = 0;
  let completedSteps = 0;
  let completedJobs = 0;
  let currentStage = null;
  for (const job of relevant) {
    const steps = Array.isArray(job.steps) ? job.steps : [];
    totalSteps += Math.max(1, steps.length);
    completedSteps += steps.length
      ? steps.filter((step) => step.status === "completed").length
      : (job.status === "completed" ? 1 : 0);
    if (job.status === "completed") completedJobs += 1;
    if (job.status === "in_progress") {
      const activeStep = steps.find((step) => step.status === "in_progress");
      if (!currentStage) {
        currentStage = activeStep
          ? (job.name || "Workflow job") + ": " + activeStep.name
          : (job.name || "Workflow job") + " running";
      }
    }
  }
  if (totalSteps === 0) return null;
  let percentage = Math.floor(100 * completedSteps / totalSteps);
  // Jobs can be added later in a matrix workflow. Never imply the workflow
  // reached 100% before GitHub declares the complete run terminal.
  if (!SCAN_ACTIVITY_TERMINAL.has(runStatus)) percentage = Math.min(99, percentage);
  return {
    percentage,
    completedSteps,
    totalSteps,
    completedJobs,
    knownJobs: relevant.length,
    currentStage,
  };
}

function selectedActivityRuns(allRuns) {
  const relevant = (Array.isArray(allRuns) ? allRuns : [])
    .filter(trackerWorkflowRun)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const active = relevant.filter((run) => run.status !== "completed").slice(0, 3);
  const finished = relevant.filter((run) => run.status === "completed");
  const recentTracker = finished.find((run) =>
    run.path === ".github/workflows/pages.yml" ||
    run.name === "Scan and publish tracker");
  const recentHotlist = finished.find((run) =>
    run.path === ".github/workflows/hotlist.yml" ||
    String(run.name || "").startsWith("Fast checks"));
  const picked = [...active, recentTracker, recentHotlist].filter(Boolean);
  const ids = new Set();
  return picked.filter((run) => {
    if (ids.has(run.id)) return false;
    ids.add(run.id);
    return true;
  }).slice(0, 5);
}

function trackerLocalTime(raw) {
  if (!raw) return "—";
  const time = Date.parse(raw);
  if (!Number.isFinite(time)) return "—";
  return new Date(time).toLocaleString("en-IE", {
    timeZone: "Europe/Dublin",
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    timeZoneName: "short",
  });
}

function trackerElapsedTime(run) {
  const start = Date.parse(run.run_started_at || run.created_at || "");
  if (!Number.isFinite(start)) return null;
  const finish = run.status === "completed"
    ? Date.parse(run.updated_at || "")
    : Date.now();
  if (!Number.isFinite(finish) || finish < start) return null;
  const wholeMinutes = Math.floor((finish - start) / 60000);
  return wholeMinutes >= 60
    ? Math.floor(wholeMinutes / 60) + "h " + (wholeMinutes % 60) + "m"
    : wholeMinutes + "m";
}

function trackerText(tag, text, cssName) {
  const element = document.createElement(tag);
  if (cssName) element.className = cssName;
  element.textContent = text;
  return element;
}

function trackerSafeRunUrl(run) {
  const id = Number(run?.id);
  if (!Number.isSafeInteger(id) || id <= 0) {
    return "https://github.com/" + SCAN_ACTIVITY_REPOSITORY + "/actions";
  }
  return "https://github.com/" + SCAN_ACTIVITY_REPOSITORY + "/actions/runs/" + id;
}

// Animate only between *actual GitHub-reported* percentages. Never simulate
// progress while a long-running crawl step is still in progress.
function trackerProgressTransition(previous, reported) {
  const target = Number.isFinite(Number(reported))
    ? Math.max(0, Math.min(100, Math.round(Number(reported)))) : 0;
  const known = previous !== null && previous !== undefined && Number.isFinite(Number(previous));
  const last = known
    ? Math.max(0, Math.min(100, Math.round(Number(previous)))) : 0;
  return {
    from: target > last ? last : target,
    to: target,
    animate: target > last,
  };
}

function trackerTweenPercent(from, to, fraction) {
  const t = Math.max(0, Math.min(1, Number(fraction) || 0));
  // Ease out, finishing at the real new percentage in 900ms.
  const eased = 1 - Math.pow(1 - t, 3);
  return Math.round(from + (to - from) * eased);
}

function animateTrackerBar(fill, label, previous, reported) {
  const change = trackerProgressTransition(previous, reported);
  const setVisual = (pct) => {
    fill.style.width = pct + "%";
    label.textContent = pct + "%";
  };
  setVisual(change.from);

  const reduceMotion = typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!change.animate || reduceMotion || typeof window.requestAnimationFrame !== "function") {
    setVisual(change.to);
    return;
  }

  window.requestAnimationFrame((startedAt) => {
    const duration = 900;
    const frame = (now) => {
      // If the user navigates away or a new run list replaces this card,
      // stop updating a detached progress element.
      if (!fill.isConnected) return;
      const fraction = Math.min(1, Math.max(0, (now - startedAt) / duration));
      setVisual(trackerTweenPercent(change.from, change.to, fraction));
      if (fraction < 1) window.requestAnimationFrame(frame);
      else setVisual(change.to);
    };
    frame(startedAt);
  });
}

function renderScanActivity(runs, detailsById, failure) {
  const root = document.getElementById("scan-activity-runs");
  const info = document.getElementById("scan-activity-summary");
  if (!root || !info) return;
  if (failure && !runs.length) {
    info.textContent = "Live GitHub status unavailable (" + failure +
      "). Open Actions to see the current runs.";
    return;
  }
  const active = runs.filter((run) => run.status !== "completed");
  const failed = runs.filter((run) => run.status === "completed" &&
     !["success", "skipped"].includes(run.conclusion));
  info.textContent = active.length
    ? active.length + " active/queued workflow" + (active.length === 1 ? "" : "s") +
      (failed.length ? " · " + failed.length + " recently failed" : "") +
      " · Checked " + trackerLocalTime(new Date().toISOString())
    : "No active runs among the latest tracker workflows · Checked " +
      trackerLocalTime(new Date().toISOString());
  if (failure) info.textContent += " · GitHub status may be delayed: " + failure;
  // Cards are recreated on each API refresh; remember the last *reported*
  // value by run ID so unchanged percentages don't animate from zero again.
  const lastReported = new Map();
  for (const oldCard of root.querySelectorAll(".scan-run[data-run-id]")) {
    const oldBar = oldCard.querySelector('[role="progressbar"][aria-valuenow]');
    if (oldBar) lastReported.set(oldCard.dataset.runId, Number(oldBar.getAttribute("aria-valuenow")));
  }
  root.replaceChildren();
  if (!runs.length) {
    root.appendChild(trackerText("p", "No recent tracker runs were returned by GitHub.", "empty"));
    return;
  }
  for (const run of runs) {
    const panel = document.createElement("article");
    panel.className = "scan-run";
    panel.dataset.runId = String(run.id);

    const heading = document.createElement("div");
    heading.className = "scan-run-head";
    const name = document.createElement("a");
    name.href = trackerSafeRunUrl(run);
    name.target = "_blank";
    name.rel = "noopener noreferrer";
    name.textContent = trackerWorkflowName(run) +
      (Number.isSafeInteger(Number(run.run_number)) ? " #" + run.run_number : "");
    heading.appendChild(name);

    const state = trackerStatus(run);
    const badge = trackerText("span", state.text, "badge " + state.css);
    heading.appendChild(badge);
    panel.appendChild(heading);

    const timing = trackerLocalTime(run.run_started_at || run.created_at);
    const elapsed = trackerElapsedTime(run);
    panel.appendChild(trackerText(
      "p",
      "Started " + timing + (elapsed ? " · " + elapsed + " elapsed" : "") +
       " · " + (run.event || "workflow"),
      "scan-run-subtitle"
    ));

    const known = detailsById.get(run.id);
    const progress = known ? trackerStepProgress(known.jobs, run.status) : null;
    const completedOk = run.status === "completed" && run.conclusion === "success";
    // Successful completion is a real 100% milestone, even when GitHub
    // stops exposing the earlier per-step details.
    const reportedPercentage = completedOk ? 100 : progress?.percentage;
    if ((progress && run.status !== "completed") || completedOk) {
      const line = document.createElement("div");
      line.className = "scan-progress-line";
      const bar = document.createElement("div");
      bar.className = "scan-progress-track";
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-label", "Completed known GitHub workflow steps");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(reportedPercentage));
      bar.setAttribute("aria-valuetext", completedOk
        ? "Workflow completed successfully"
        : progress.completedSteps + " of " + progress.totalSteps + " reported steps complete");
      const fill = document.createElement("div");
      fill.className = "scan-progress-fill";
      fill.setAttribute("aria-hidden", "true");
      bar.appendChild(fill);
      const percentageLabel = trackerText("strong", reportedPercentage + "%");
      line.appendChild(bar);
      line.appendChild(percentageLabel);
      panel.appendChild(line);
      // The DOM must be connected before checking isConnected in the animator.
      // The new value is the only destination: no invented in-between scan work.
      const oldValue = lastReported.get(String(run.id));
      panel._animateProgress = () => animateTrackerBar(
        fill, percentageLabel,
        completedOk && oldValue === undefined ? 100 : oldValue,
        reportedPercentage
      );
      if (completedOk) {
        panel.appendChild(trackerText("p",
          "Workflow completed successfully. This is not an Amazon product count.",
          "scan-run-step"
        ));
      } else {
        panel.appendChild(trackerText("p",
          progress.completedSteps + " of " + progress.totalSteps +
          " reported steps completed · " + progress.completedJobs + " of " +
          progress.knownJobs + " jobs finished",
          "hint"
        ));
        if (progress.currentStage) {
          panel.appendChild(trackerText("p", progress.currentStage, "scan-run-step"));
        }
      }
    } else if (run.status === "completed") {
      panel.appendChild(trackerText("p",
        "Workflow finished: " + (run.conclusion || "unknown") +
          ". Check the GitHub logs for details.",
        "scan-run-step"
      ));
    } else {
      panel.appendChild(trackerText("p",
        "GitHub hasn't reported detailed job progress yet. " +
        "Open the run for its current step.",
        "scan-run-step"
      ));
    }
    root.appendChild(panel);
    if (panel._animateProgress) {
      panel._animateProgress();
      delete panel._animateProgress;
    }
  }
}

let trackerActivityLastRuns = [];
const trackerActivityJobCache = new Map();
let trackerActivityLastFetchAt = 0;
let trackerActivityFetching = false;

async function trackerGithubGet(uri) {
  const response = await fetch(uri, {
    headers: { "Accept": "application/vnd.github+json" },
    cache: "no-store",
  });
  if (!response.ok) {
    if (response.status === 403 || response.status === 429) {
      throw new Error("GitHub API rate limit");
    }
    throw new Error("GitHub API HTTP " + response.status);
  }
  return response.json();
}

async function updateScanActivity() {
  if (trackerActivityFetching || (typeof document !== "undefined" && document.hidden)) return;
  trackerActivityFetching = true;
  try {
    const payload = await trackerGithubGet(SCAN_ACTIVITY_API + "/actions/runs?per_page=35");
    const runList = selectedActivityRuns(payload.workflow_runs || []);
    trackerActivityLastRuns = runList;
    trackerActivityLastFetchAt = Date.now();

    const fetchDetails = runList.filter((run) => run.status !== "completed")
      .slice(0, SCAN_ACTIVITY_MAX_ACTIVE_DETAILS);
    let jobsError = "";
    for (const run of fetchDetails) {
      const previous = trackerActivityJobCache.get(run.id);
      if (previous && Date.now() - previous.fetchedAt < SCAN_ACTIVITY_JOBS_MS) continue;
      try {
        const info = await trackerGithubGet(
          SCAN_ACTIVITY_API + "/actions/runs/" + encodeURIComponent(run.id) + "/jobs?per_page=100"
        );
        trackerActivityJobCache.set(run.id, {
          jobs: info.jobs || [],
          fetchedAt: Date.now(),
        });
      } catch (error) {
        jobsError = error.message;
        // Retry list polling normally but delay repeated jobs calls after a rate
        // limit or temporary GitHub outage.
        trackerActivityJobCache.set(run.id, {
          jobs: previous?.jobs || [],
          fetchedAt: Date.now(),
        });
        if (jobsError === "GitHub API rate limit") break;
      }
    }
    renderScanActivity(runList, trackerActivityJobCache, jobsError);
  } catch (error) {
    renderScanActivity(
      trackerActivityLastRuns, trackerActivityJobCache, error.message || "unavailable"
    );
  } finally {
    trackerActivityFetching = false;
  }
}

function startScanActivity() {
  if (!document.getElementById("scan-activity-runs")) return;
  updateScanActivity();
  window.setInterval(updateScanActivity, SCAN_ACTIVITY_POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden &&
        Date.now() - trackerActivityLastFetchAt >= SCAN_ACTIVITY_POLL_MS) {
      updateScanActivity();
    }
  });
}

// Node tests import pure parsers; the browser runs the network/UI lifecycle.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    trackerWorkflowRun, trackerWorkflowName, trackerStatus,
    trackerStepProgress, selectedActivityRuns, trackerSafeRunUrl,
    trackerProgressTransition, trackerTweenPercent,
  };
}
if (typeof document !== "undefined") {
  startScanActivity();
}
