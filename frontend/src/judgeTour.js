import { driver } from 'driver.js'
import 'driver.js/dist/driver.css'

// Guided judge walkthrough (driver.js).
// The app renders one `view` at a time, so every step carries the view it
// belongs to. Custom next/prev handlers switch the view and poll until the
// target element has mounted before advancing — a bare timeout is racy.

const STEPS = [
  {
    view: 'dashboard',
    popover: {
      title: 'Welcome, Judges',
      description:
        'This guided tour shows how to load the evaluation dataset, reset the system, run the pipeline cold, and verify the score.',
    },
  },
  {
    view: 'dashboard',
    popover: {
      title: 'Adding the test dataset',
      description:
        'Drop the provided inbox/email_*.json files into sdoc-hackathon-bundle/inbox/ and their files into sdoc-hackathon-bundle/attachments/. Each email JSON has email_id, from, subject, body, attachments (paths relative to the bundle root). New emails appear in the Inbox automatically — no other setup.',
    },
  },
  {
    element: '[data-tour="nav-reset"]',
    view: 'dashboard',
    popover: {
      title: 'Step 1: Reset Demo',
      description:
        'Wipes every verdict, the audit trail, and the Supabase records for a true cold start — while preserving the 520 inbox emails and the LLM response cache so the re-run stays fast.',
      side: 'right',
    },
  },
  {
    element: '[data-tour="reset-scope"]',
    view: 'reset',
    popover: {
      title: 'What gets wiped',
      description:
        'Local stores + submission.json + audit state + Supabase verifications and audit logs. Deliberately preserved: inbox and the LLM cache — that\u2019s what makes the demo re-run fast.',
    },
  },
  {
    element: '[data-tour="reset-confirm"]',
    view: 'reset',
    popover: {
      title: 'Typed confirmation',
      description:
        'Type RESET to arm the button — no accidental wipes on stage. A dry-run preview shows the exact blast radius first.',
    },
  },
  {
    element: '[data-tour="nav-pipeline"]',
    view: 'reset',
    popover: {
      title: 'Step 2: Run the pipeline',
      description: 'Next up: the Submission Builder.',
      side: 'right',
    },
  },
  {
    element: '[data-tour="pipeline-run"]',
    view: 'pipeline',
    popover: {
      title: 'Cold-start run',
      description:
        'Every inbox email is classified (Laya neural triage), fields extracted by deterministic regex first — only blanks escalate to one cached LLM call — then SI and BL are compared on 7 fields.',
    },
  },
  {
    element: '#score-card',
    view: 'pipeline',
    popover: {
      title: 'Score vs ground truth',
      description:
        'Score it grades submission.json against ground truth — the same artifact the organizers score.',
    },
  },
  {
    element: '[data-tour="nav-dashboard"]',
    view: 'pipeline',
    popover: {
      title: 'Step 3: Results',
      description: 'Back to the dashboard for the live numbers.',
      side: 'right',
    },
  },
  {
    element: '[data-tour="dashboard-kpis"]',
    view: 'dashboard',
    popover: {
      title: 'Live KPIs',
      description: 'OK / MISMATCH / NEEDS_REVIEW breakdown.',
    },
  },
  {
    element: '[data-tour="nav-queue"]',
    view: 'dashboard',
    popover: {
      title: 'Inbox',
      description:
        'Per-email verdicts with defect fields, SI vs BL side-by-side, and provenance tags (rule vs model) per field.',
      side: 'right',
    },
  },
  {
    element: '[data-tour="nav-stress"]',
    view: 'queue',
    popover: {
      title: 'Stress Lab',
      description: 'Next up: the adversarial edge-case battery.',
      side: 'right',
    },
  },
  {
    element: '[data-tour="stress-run"]',
    view: 'stress',
    popover: {
      title: 'Edge-case battery',
      description:
        '2,000 adversarial cases: corrupt attachments, prompt injections, boundary values. Generate with python tests/generate_stress_dataset.py, then run here.',
    },
  },
  {
    element: '[data-tour="nav-assistant"]',
    view: 'stress',
    popover: {
      title: 'AI Assistant',
      description:
        'Tool-calling agent over the whole dataset; write actions (verify, chaser emails, pipeline) are approval-gated.',
      side: 'right',
    },
  },
  {
    view: 'chat',
    popover: {
      title: 'That\u2019s it',
      description:
        'Reset → add dataset → run → score. submission.json is the scored artifact. Questions welcome.',
    },
  },
]

let tour = null // active driver.js instance (guard against double-start)

export function startJudgeTour(setView) {
  if (tour) {
    tour.destroy()
    tour = null
  }

  let started = false // becomes true once d.drive() has run
  let navPending = false // a view-switch navigation is in flight

  const d = driver({
    showProgress: true,
    animate: true,
    overlayOpacity: 0.75,
    stagePadding: 8,
    stageRadius: 10,
    nextBtnText: 'Next →',
    prevBtnText: '← Back',
    doneBtnText: 'Done',
    steps: STEPS,
    onNextClick: (_element, _step, opts) => {
      const target = STEPS[(opts.state.activeIndex ?? 0) + 1]
      // On the last step the "Done" button routes here — close instead of advancing.
      if (!target) {
        opts.driver.destroy()
        return
      }
      goToStep(target, () => opts.driver.moveNext())
    },
    onPrevClick: (_element, _step, opts) => {
      const target = STEPS[(opts.state.activeIndex ?? 0) - 1]
      // movePrevious() at index 0 would destroy the tour — ignore instead.
      if (!target) return
      goToStep(target, () => opts.driver.movePrevious())
    },
    onDestroyed: () => {
      if (tour === d) tour = null
      navPending = false
      setView('dashboard')
    },
  })

  // Switch to the target step's view, wait for its element to mount, then
  // proceed. React renders async, so poll rather than assume.
  function goToStep(target, proceed) {
    if (navPending) return
    navPending = true
    setView(target.view)

    // Centered steps have no element — a short settle delay is enough.
    if (!target.element) {
      setTimeout(() => {
        if (tour !== d) return // superseded by a newer tour
        if (started && !d.isActive()) return // closed while waiting
        navPending = false
        proceed()
      }, 150)
      return
    }

    const deadline = Date.now() + 2000
    const poll = () => {
      // Tour was closed while we were waiting — stop.
      if (started && !d.isActive()) {
        navPending = false
        return
      }
      // A newer tour has replaced this one — don't interfere.
      if (tour !== d) return
      if (document.querySelector(target.element) || Date.now() > deadline) {
        navPending = false
        proceed()
      } else {
        setTimeout(poll, 100)
      }
    }
    poll()
  }

  tour = d
  goToStep(STEPS[0], () => {
    started = true
    d.drive()
  })
}
