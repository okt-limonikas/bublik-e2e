// A Playwright reporter that writes the run as it goes, one JSON line per
// event, to the file BUBLIK_LIVE_EVENTS names (nothing without it), for
// `bublik-e2e playwright-live` to stream into Bublik. It only records: the
// browser's suite tree once the run begins, each test's final result in the
// JSON reporter's shape, and the end.
import fs from "node:fs";
import path from "node:path";

const browser = process.env.BUBLIK_LIVE_BROWSER || process.env.JOB_BROWSER || "";

function relative(rootDir, location) {
  if (!location) return { file: "", line: 0, column: 0 };
  return {
    file: path.relative(rootDir, location.file).split(path.sep).join("/"),
    line: location.line,
    column: location.column,
  };
}

function serializeSuite(rootDir, suite) {
  if (!suite.allTests().length) return null;
  const suites = suite.suites.map((s) => serializeSuite(rootDir, s)).filter(Boolean);
  return {
    title: suite.title,
    ...relative(rootDir, suite.location),
    specs: suite.tests.map((test) => ({
      title: test.title,
      id: test.id,
      tags: test.tags.map((tag) => tag.replace(/^@/, "")),
      ...relative(rootDir, test.location),
    })),
    ...(suites.length ? { suites } : {}),
  };
}

function serializeStep(step) {
  const steps = (step.steps || []).filter((s) => s.category === "test.step");
  return {
    title: step.title,
    duration: step.duration,
    ...(step.error ? { error: { message: step.error.message || String(step.error) } } : {}),
    ...(steps.length ? { steps: steps.map(serializeStep) } : {}),
  };
}

function serializeResult(result) {
  const steps = (result.steps || []).filter((s) => s.category === "test.step");
  return {
    status: result.status,
    duration: result.duration,
    retry: result.retry,
    startTime: result.startTime.toISOString(),
    errors: (result.errors || []).map((e) => ({ message: e.message || e.value || "" })),
    attachments: (result.attachments || [])
      .filter((a) => a.path)
      .map((a) => ({ name: a.name, contentType: a.contentType, path: a.path })),
    ...(steps.length ? { steps: steps.map(serializeStep) } : {}),
  };
}

// The attempt that settles a test: Playwright retries one that did not end as
// expected while it has retries left.
export function isFinal(test, result) {
  if (["skipped", "interrupted"].includes(result.status)) return true;
  if (result.status === test.expectedStatus) return true;
  return result.retry >= test.retries;
}

export default class BublikLiveReporter {
  constructor() {
    this.file = process.env.BUBLIK_LIVE_EVENTS || "";
    this.rootDir = "";
  }

  write(event) {
    if (!this.file) return;
    try {
      fs.appendFileSync(this.file, JSON.stringify(event) + "\n");
    } catch (error) {
      // Live import is a bonus: it never fails the run.
      console.warn(`bublik live reporter: ${error.message}`);
      this.file = "";
    }
  }

  onBegin(config, suite) {
    this.rootDir = config.rootDir;
    const project = suite.suites.find((s) => s.project()?.name === browser);
    const suites = project
      ? project.suites.map((s) => serializeSuite(this.rootDir, s)).filter(Boolean)
      : [];
    this.write({ type: "begin", time: new Date().toISOString(), browser, suites });
  }

  onTestEnd(test, result) {
    if (test.parent.project()?.name !== browser || !isFinal(test, result)) return;
    this.write({
      type: "test",
      id: test.id,
      test: {
        projectName: browser,
        expectedStatus: test.expectedStatus,
        status: test.outcome(),
        results: test.results.map(serializeResult),
      },
    });
  }

  onEnd(result) {
    this.write({ type: "end", time: new Date().toISOString(), status: result.status });
  }

  printsToStdio() {
    return false;
  }
}
