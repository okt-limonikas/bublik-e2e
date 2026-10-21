// A Playwright reporter that writes the run as it goes, one JSON line per
// event, to the file BUBLIK_LIVE_EVENTS names (nothing without it), for
// `bublik-e2e playwright-live` to stream into Bublik. It only records: each
// browser project's suite tree once the run begins, each test's final result
// in the JSON reporter's shape, and the end. The browser projects are the
// ones BUBLIK_LIVE_BROWSER or JOB_BROWSER list (comma-separated), else every
// project no other one depends on (not a setup project).
import fs from "node:fs";
import path from "node:path";

const named = (process.env.BUBLIK_LIVE_BROWSER || process.env.JOB_BROWSER || "")
  .split(",")
  .map((name) => name.trim())
  .filter(Boolean);

// The projects the run's browser projects are, in the config's order.
export function browserProjects(config, named) {
  const setup = new Set();
  for (const project of config.projects) {
    for (const dependency of project.dependencies || []) setup.add(dependency);
    if (project.teardown) setup.add(project.teardown);
  }
  return config.projects
    .map((project) => project.name)
    .filter((name) => (named.length ? named.includes(name) : !setup.has(name)));
}

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
    this.browsers = [];
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
    this.browsers = browserProjects(config, named);
    const projects = [];
    for (const name of this.browsers) {
      const project = suite.suites.find((s) => s.project()?.name === name);
      const suites = project
        ? project.suites.map((s) => serializeSuite(this.rootDir, s)).filter(Boolean)
        : [];
      if (suites.length) projects.push({ project: name, suites });
    }
    this.write({ type: "begin", time: new Date().toISOString(), projects });
  }

  onTestEnd(test, result) {
    const project = test.parent.project()?.name;
    if (!this.browsers.includes(project) || !isFinal(test, result)) return;
    this.write({
      type: "test",
      id: test.id,
      test: {
        projectName: project,
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
