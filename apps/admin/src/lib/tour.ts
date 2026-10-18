// Feature tours: a short guided walk through the page for a first-time visitor, owner or operator.
// Each step points at an element marked with data-tour="..."; steps whose element is not on the page are skipped.
// A tour starts once on the first visit (remembered in this browser) and can be replayed from "Take the tour".
import { driver, type DriveStep } from "driver.js";
import "driver.js/dist/driver.css";
import { useEffect } from "react";

export type TourName = "demo" | "owner" | "console";

type Step = { target: string; title: string; text: string; side?: "top" | "bottom" | "left" | "right" };

const TOURS: Record<TourName, Step[]> = {
  demo: [
    {
      target: "demo-customer",
      title: "You are the customer",
      text: "Type here as a customer would on WhatsApp: ask a price, order a cake, change a date. The AI answers for the shop.",
    },
    {
      target: "demo-owner",
      title: "This is the owner's phone",
      text: "Meera gets a message when the AI needs her: a question it cannot answer, an action it may not take alone. Reply here as the owner.",
    },
    {
      target: "demo-guide",
      title: "Things to try",
      text: "Tap a suggestion to send it. Try an order with payment, a question the shop never answered, and a phone call.",
      side: "right",
    },
    {
      target: "demo-hood",
      title: "Under the hood",
      text: "Every message, check and decision appears here as it happens, so you can see why the AI said what it said.",
      side: "right",
    },
    {
      target: "demo-dashboard",
      title: "The owner dashboard",
      text: "Open the dashboard Meera uses: her inbox, orders, and everything the AI has learned from her.",
      side: "bottom",
    },
    {
      target: "demo-restart",
      title: "Start over any time",
      text: "This gives you a fresh copy of the shop, with nothing from your earlier tries.",
      side: "bottom",
    },
  ],
  owner: [
    {
      target: "nav-home",
      title: "Your day at a glance",
      text: "Messages answered, orders taken, and anything waiting for you.",
      side: "right",
    },
    {
      target: "nav-try",
      title: "Try and teach",
      text: "Set your shop up by chatting, and test it as a customer would before anyone real writes to you.",
      side: "right",
    },
    {
      target: "nav-inbox",
      title: "Inbox",
      text: "Every chat and call with your customers. Step in on any chat: the AI waits while you reply.",
      side: "right",
    },
    {
      target: "nav-orders",
      title: "Orders",
      text: "Orders the AI took, their payments, and how full each day is.",
      side: "right",
    },
    {
      target: "nav-brain",
      title: "Brain",
      text: "What the AI has learned from you: answers, rules, procedures and your catalogue. Anything it learned can be undone.",
      side: "right",
    },
    {
      target: "tour-button",
      title: "See this again",
      text: "Take the tour again any time from here.",
      side: "bottom",
    },
  ],
  console: [
    {
      target: "business",
      title: "Choose a shop",
      text: "Every page shows the shop picked here.",
      side: "bottom",
    },
    {
      target: "nav-home",
      title: "Home",
      text: "The shop's day at a glance: replies, orders and anything waiting for a person.",
      side: "right",
    },
    {
      target: "nav-inbox",
      title: "Inbox",
      text: "Every conversation, call and customer. Open one to see the full timeline.",
      side: "right",
    },
    {
      target: "nav-orders",
      title: "Orders",
      text: "Orders, payments and daily capacity.",
      side: "right",
    },
    {
      target: "nav-brain",
      title: "Brain",
      text: "The shop's knowledge, rules and procedures, with their history and undo.",
      side: "right",
    },
    {
      target: "nav-quality",
      title: "Quality",
      text: "The simulation lab, X-ray traces of every message, chaos tests, the evaluation suites, costs and benchmarks.",
      side: "right",
    },
    {
      target: "nav-settings",
      title: "Settings",
      text: "Channels, the demo reset, runtime flags and the access logs.",
      side: "right",
    },
    {
      target: "tour-button",
      title: "See this again",
      text: "Take the tour again any time from here.",
      side: "bottom",
    },
  ],
};

const SEEN = (name: TourName) => `reflexo.tour.${name}.v1`;

function seen(name: TourName): boolean {
  try {
    return localStorage.getItem(SEEN(name)) === "1";
  } catch {
    return true; // no storage: never start on its own, the button still works
  }
}

function markSeen(name: TourName): void {
  try {
    localStorage.setItem(SEEN(name), "1");
  } catch {
    /* storage unavailable */
  }
}

/** Run a tour now, with the steps whose elements are on the page. */
export function startTour(name: TourName): void {
  const steps: DriveStep[] = TOURS[name]
    .filter((s) => document.querySelector(`[data-tour="${s.target}"]`))
    .map((s) => ({
      element: `[data-tour="${s.target}"]`,
      popover: { title: s.title, description: s.text, side: s.side ?? "left", align: "start" },
    }));
  if (!steps.length) return;
  markSeen(name);
  driver({
    popoverClass: "reflexo-tour",
    showProgress: true,
    allowClose: true,
    nextBtnText: "Next",
    prevBtnText: "Back",
    doneBtnText: "Done",
    progressText: "{{current}} of {{total}}",
    steps,
  }).drive();
}

/** Start the tour on a first visit, once the page has drawn its marked elements. */
export function useFirstVisitTour(name: TourName, ready = true): void {
  useEffect(() => {
    if (!ready || seen(name)) return;
    const timer = window.setTimeout(() => startTour(name), 800);
    return () => window.clearTimeout(timer);
  }, [name, ready]);
}
