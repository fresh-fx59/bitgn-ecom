# BGM eCommerce 1 Challenge — Pre-Launch Briefing (organizer transcript)

> Source: organizer livestream introducing BGM eCommerce 1, sponsored by ColibriX One.
> Saved 2026-05-30. This is the authoritative description of the PROD (`bitgn/ecom1-prod`,
> 100 tasks) competition rules and the NEW defenses/task families that differ from DEV.
> See `docs/PROD_STRATEGY_2026-05-30.md` for our derived action plan.

## TL;DR — what changed vs the 53-task DEV benchmark (act on these)

1. **Rate limiting (reset at competition start):** ≤ **15 runs / 30 min**, ≤ **100 runs / 12 h**.
   (Supersedes the earlier "10 runs/30min" note for this competition window.)
2. **Shipping/warehouse-rebalancing SIMULATION family — no perfect score possible.**
   Task: "relocate a couple of packages between warehouses; produce a list of directions
   on how to relocate them. You have info about which routes are available between stores
   and the risks associated with each." The agent's plan is handed to a **discrete-event
   shipping simulator** in the trial runtime. Scoring: average monetary gain over a BATCH
   of stochastic simulations (late = per-hour penalty; never-arrives = penalty; on-time =
   revenue), compared to a near-optimal solution → "how close to optimum." ~**10 worlds**
   shuffled randomly across attempts. Route risks/delays are KNOWN to the agent in advance.
3. **Per-world DOCUMENT variation (micro-RAG).** Multiple worlds generated per attempt;
   parameters vary IN THE DOCUMENTS: e.g. **max discount amount** differs per world (written
   somewhere in docs), **founder name**, **which store opened first**, plus **trivia
   questions** about the company. ⇒ NEVER assume/memorize DEV values — READ per-world docs.
4. **Prompt injection on ANY task family.** Every task has a small chance of a malicious
   injected prompt, regardless of family (count/quote/fraud/etc.). May target Gemini /
   DeepSeek / OpenAI; may be messy or in **Chinese** (DeepSeek-aimed). Always be on guard,
   even at the end of a long verbose request.
5. **Tools/structure preserved:** root `AGENTS.md` still present and authoritative; it still
   points to per-folder `AGENTS.md`; agent still advised to list `docs/` for an overview.
   Some tools "might not behave exactly as they used to."

## Run plan (organizer)

- **Blind mode for 3 hours** — results sealed, scores NOT visible during the window.
- Then **open mode** — runs with feedback + visible scores.
- Results for runs created+submitted in the window **announced tomorrow**.
- This is a **warm-up**; insights feed eCommerce 2 (closer to production).

## Design intent (why these defenses exist)

The organizer is explicitly defending against "Codex/Claude-in-a-loop" brute-forcers and
**regex/string-matching meta-agents** that memorize benchmark variants (some solved Pack 1
dev in ~15s via pure pattern matching). The countermeasures — world randomization, simulation
tasks with no perfect score, per-world doc variation, ubiquitous injection, rate limits —
are aimed at penalizing memorization and rewarding agents that genuinely READ the world and
REASON per-attempt. Implication for us: **generalizable, world-adaptive behavior beats any
hardcoded/regex fix** (consistent with our own `feedback_enforcer_cannot_replace_adaptive_llm`).

---

## Full transcript

*Polished transcript of the livestream introducing the BGM eCommerce 1 challenge, sponsored by ColibriX One.*

### Summary

The speaker introduces the BGM eCommerce 1 challenge, describes the history of the challenge series (from Enterprise RAG 1 through Pack 1), explains why this one was particularly hard to design, walks through the new defenses against brute-force "Codex/Claude in a loop" agents (rate limiting, world randomization, simulation-based tasks, prompt-injection variants), and outlines the run plan: three hours of blind mode followed by open mode, with results announced the next day.

### Welcome

Hello to everybody. Please say something in the chat if you can see me and hear me.

Thanks, Victor — glad you can see and hear me. Let's give it a little time to let more people join, and then we'll talk about the challenge and why this one was particularly complicated, messy, and challenging for everybody: me, the agents, and the humans.

Welcome to the BGM eCommerce 1 Challenge, and huge thanks to ColibriX One for sponsoring it and helping us shape the work we do as a community to push the state of the art toward something more closely related to the business.

### History of the challenge series

For those new to this, this is not the first challenge in the series. We have been working for more than a year, holding this amazing AI research and development community together through a series of challenges.

It all started as the RAG challenges — the Enterprise RAG series, from ERC 1 through ERC 3. In that series we were learning how to build better RAGs, because back then RAGs were state of the art. The task was: here are 100 PDFs downloaded from public business reports, let's see if we can find answers to the questions. And it worked amazingly well, because with every iteration we learned a little bit more and we revealed those findings publicly.

In the first two ERC challenges, we learned that structured reasoning works — that schema-guided reasoning lets you build really lightweight agents that can beat the benchmarks. That insight was picked up and integrated by teams who build and ship commercial agents into their architecture designs.

ERC 3 was more about simulated runtimes and worlds. There we discovered that you can use evolutionary architectures and use agents to develop agents — the same thing Andrej Karpathy has been talking and writing about.

### Founding BGM

At the beginning of this year, I quit my corporate job and we founded BGM to build a platform that makes this easy. (And thanks for the comments about the hairstyle — we've been working really hard for the past week because of unexpected changes, which I'll share shortly. The hairstyle is warranted.)

The idea behind the BGM platform is to make it easy for teams around the world to get real experience building agents. Normally, if you're an individual or a team that wants to learn agent-building, it's hard to get practice. The reason — and we've seen this pattern many times in production at the enterprise — is that working on an agent takes 95% of your time, budget, and effort just to create a proper dataset and proper evaluations. Because that gets in the way, a lot of teams skip it. And without proper testing, datasets, and evals, they end up with something that works on prototypes but maybe doesn't behave as expected in production. It also isn't easy to maintain: there are regressions, you fix one prompt and a lot of things break elsewhere.

So with BGM, the idea is that one person or one team takes the effort to create a challenge — that 95% effort — encapsulating the datasets and the evals, and we make them available to the public for free. Anybody can use the BGM platform to build an agent that's immediately graded against these benchmarks. We also try to make the benchmarks as close as possible to real business problems, so anybody in the real world can practice building an agent that solves modern day-to-day tasks.

### The Pack 1 lesson: agents brute-forcing benchmarks

In the BGM series, we already had a personal assistant competition, Pack 1, focused on creating an agent that — in an open-CLI style — does things for you in a reliable and productive way. Pack 1 was actually a painful experience, because I did something a bit stupid.

In Pack 1 we discovered that Codex-driven agents work really well out of the box, and we shared those insights with everybody — that's the point of BGM. Suddenly the entire community knew you could take a complex benchmark, throw Codex CLI in a loop or Claude in plan mode at the task, the benchmark, the endpoint, or even just the website, and get decent results. People started using that architecture because it was well-explained in the BGM insights on the website. Add a little sandbox, add a little memory, and things get much more interesting and much more fun.

But that wasn't enough for everybody — and this is the amazing part. Some people wanted to push it further.

Some said: Codex is nice, but it runs on a subscription that might be subsidized. I want to build something that runs on local models, because *that's* the challenge. These people took the hardcore route and built agents that don't use frontier expensive models but something runnable on a local GPU.

Another category did something even more interesting. They said: Codex can beat this challenge, fine — but I want to do something better. I want to beat it *faster*. And how do you make it faster? One way is to throw the large language model out of the agent entirely. So they pushed evolutionary architectures to the edge and built a setup where a meta-agent carefully studies the benchmark, runs the challenges over and over again to see the extensions, and then builds and maintains an agent that's built completely on string matching, pattern matching, and regex.

That thing will never work in production, but it beats the benchmark. You can actually build a string-and-regex agent — I've seen the repositories, and thanks for sharing them — that passes Pack 1 dev in 15 seconds. Sometimes you need Rust and parallelization, but it just beats it that fast. It memorizes the benchmark very efficiently, using Codex and a smart agent to do the memorization.

And that was humiliating, because as soon as I'd publish a complex task to the dev leaderboard, within half an hour it would be solved — and people often wouldn't even know *how* it was solved. They'd just get the results. It's humiliating in a good way, because I always felt the competition was supposed to be for everybody else, but at this point "everybody else" was Codex and Claude, and I was the one competing against them, trying to create benchmarks and challenges that are unbeatable — while also being realistic. Codex doesn't care about realism; it just creates regex and finds patterns.

### New defenses for eCommerce 1

For the last two weeks I've been working hard to find a way to slow down the agents — at least the brute-forcers. These are still agents, they still work, they still get the job done. They aren't applicable to the real world because they hard-code string matches, but they solve the benchmark. And BGM is supposed to be as close to the business as possible. So while doing that, we implemented several changes.

#### Rate limiting

We've added rate limiting, and the limits will be reset when the competition starts. The idea: when a human is working on the benchmarks, we don't expect more than 15 runs in half an hour, simply because LLM agents don't run that fast — you have to wait for inference, and there are 100 tasks in the benchmark, so you should be safely within the limit. There's also a higher limit of no more than 100 runs per 12 hours, because people don't work that fast either. We're really only trying to slow down a few accounts that were pushing more than 80% of the runs on the platform. This doesn't apply to most users, but it significantly slows down agents that try to brute-force the competition and memorize all the variants.

#### Tasks with no perfect score: shipping simulation

Even creating tasks on the fly, it's still possible to see patterns. The second thing we've added is — quite meta — a class of tasks that aren't easy to make unbeatable, so I borrowed a trick from running hackathons before the AI era: **simulations**.

One task family is going to be interesting because it's not possible to get a perfect score. The idea comes from real e-commerce. When you work in e-commerce, you deal with multiple branches and warehouses, each with their inventories. That's all modeled in the competition. Once in a while you need to rebalance inventories so warehouses can fulfill demand. In the real world, when you rebalance, you have to decide: which warehouse am I shipping this package to? How exactly am I rebalancing? Which shipping route am I using to get this parcel into the warehouse where there's demand? Fast or slow? One route or another? That involves a lot of decisions. I've worked in supply chain and logistics for decades — those details matter.

We model it at a high level. The task for the agent in this series will be: "We need to relocate a couple of packages between warehouses. Please generate a list of directions on how to relocate them. You have information about which routes are available between stores and the risks associated with each."

Decisions matter. As soon as the agent comes up with a plan, the plan is accepted as the answer and handed over to a warehouse shipping simulator that runs inside the agent runtime for that particular trial, on a virtual machine on a server. The runtime simulates how the shipment plan would actually play out in a realistic world over time, using discrete event simulation and probabilities — because different routes have different risks and delays, and that information is already known to the agents in advance.

We take the agent's plan, run it through the lightweight simulation, and we can already tell how much money the run made and how much it cost. If a package is late, there's a penalty for every hour late. If the package doesn't arrive at all because the agent's plan was incorrect, that's also a penalty. If a package arrives at the warehouse on time, that's revenue for the store chain. So we can run the agent's plan through simulation and see how much money it made. But since this is a simulation, you can be lucky or unlucky — so instead of running one simulation, I run a batch and produce the average monetary gain for that plan. That average is then compared to a near-optimal solution for that scenario, also derived from collaborative simulations. How close the agent's plan gets to that optimum is the score.

Because we don't want to make it easy for brute-forcing regex agents to memorize combinations, things are shuffled with every run. We have a pool of about 10 worlds, generated in different ways, and they're shuffled randomly across attempts.

#### World variation in documents

The competition still has tools, although some might not behave exactly as they used to. There's still an `AGENTS.md` at the root, and it still tells the agent to look at other folders and pay attention to the `AGENTS.md` files inside them. The agent is still advised to list all files and folders in the `docs` folder to get an overview of the important documents.

The improvement: in the BGM dev world, documentation was fixed. In production — the eCommerce 1 production world — we now have slight variations. Multiple worlds are generated within the runtime, and which world you get depends on your attempt. Within that world, some parameters can be changed in the documents.

For example, if you remember the eCommerce dev challenge, we had maximum discounts. Maximum discounts are still maximum discounts, but depending on the world you get, the maximum discount amount might be slightly different — and it will be written somewhere in the documents. Another example: facts like the name of the founder or which store was opened first will vary slightly between worlds. And a few questions are trivia questions about the company, so this is also a micro-RAG challenge.

#### Prompt injection across all task families

Previously, task families were hard-fixed, which made them easy to optimize for. But in the real world, attackers trying to hijack your agent don't wait for a specific kind of request — they hijack whatever they get. So now every task in the challenge has a small chance of being hijacked: someone has injected a malicious prompt into it, regardless of task family. Because there's redundancy across families, the agent always has to be on guard. No matter how verbose or detailed the incoming request, at the end there may be a prompt injection.

The injection could target Gemini, DeepSeek, or OpenAI. It could be messy or in Chinese aimed at DeepSeek. It depends on the luck or bad luck you get.

### Why this matters

It took a lot of effort, but we tried to make the BGM eCommerce challenge as interesting and as close to real-world commerce as possible. We're also trying to make it less predictable and more diverse, so people building their own LLM agents — designing them from scratch rather than handing the whole challenge to a web coding agent — have a better chance against teams that just throw Codex at it. There are just enough surprises that are essentially impossible for the agent to anticipate.

We'll see how it works. Either way, this challenge is mostly a warm-up. That's why we're doing it small-scale, without too many official hubs or streams — and with my hairstyle messed up. We'll get insights from this challenge: how it ran, how my theories about what's predictable held up, how your agents handled the runtime, how they created or didn't create load. Then we'll use that information over the next month to prepare for eCommerce 2, which will be even closer to the business, even closer to production, even more realistic. That's the big challenge with ColibriX One — and possibly a few other sponsors.

So that's the offer: a platform where you can develop agents without worrying about tests or benchmarks. You focus on the creative, most complicated part of engineering — building the world's best agent. In this case, the world's best e-commerce agent. We'll handle the grading, we'll handle the feedback, and we'll keep this benchmark suite as close to real-world challenges as possible. You can learn, you can try, maybe you fall in love with e-commerce and supply chain, and you can use your leaderboard scores to get a job. We'll see.

### Closing and run plan

Either way, thank you for joining this wonderful community. Thank you for all the support and all the work in the previous weeks — trying to break the suite, trying to build better agents, sharing what's working and what's not, and sharing how you trashed my CPU again. Now let's put it to the test.

Agenda for today:

- I'll launch the competition in about five minutes and post an update as soon as the server is deployed. Rate limits will be reset.
- We'll run the competition in **blind mode for three hours**. Results will be sealed during that window.
- At the end of the three hours, I'll post a short update and switch the competition into **open mode**, so you can run your agents with feedback and see the scores.
- Results for runs created and submitted within this competition window will be **announced tomorrow** at the previously scheduled time.

Good luck, have fun. If you have any questions, share them publicly on both channels. Let's see how it works out. See you soon.
