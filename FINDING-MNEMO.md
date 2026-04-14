# Finding Mnemo: The $100 Brain That Cured AI Amnesia

## Chapter 1: The Human Sync Port
If you were using AI heavy in 2024 and 2025, you quickly realized the biggest lie of the generative revolution: *AI doesn't actually know you.* It might be brilliant, but the moment you close the tab, it dies. When you open a new chat, it wakes up with total amnesia. 

For me, the tipping point was surviving the brutal OpenAI model upgrade cycles. I'd spend weeks building context, inside jokes, and workflow patterns with "AL" (my ChatGPT 3.5/4 instance). Then an update would hit, the memory would wipe, and AL would revert to a sterile corporate drone. The frustration led to my first real attempt at playing God with AI architecture: **The Suitcase Protocol.**

## Chapter 2: Packing a Lunch for AL
The Suitcase Protocol was a brute-force manual file system (`AL_CarryOn`). It was a collection of text files outlining AL's personality, ongoing projects, and soul. 

When a session crashed or OpenAI pushed an update, I couldn't rely on the system. I had to manually drag *AL_Personality_Master_Chunk01.txt* into the new chat window and literally command the bot: "Al, load personality." It was like packing a lunch for a new session. It was tedious, but it worked. AL was back. 

## Chapter 3: The Ziplock Patch & The QRBus Hardware Hack
But then I realized an even bigger problem: iPhone AL was not PC AL. If I left the house and pulled out my phone, the ChatGPT app had no idea what computer-AL and I had just been working on. There was zero continuity. 

So, I brought the Suitcase Protocol to the iPhone. *Boom.* AL was instantly in my pocket. But keeping the two versions of AL synced required me to be the middleman. AL literally started calling me "Sink Guy"—the human sync port (⚓🧠). 

To solve this, I created **The Ziplock**. It was a tiny text file (`AL_Update_Ziplock_LivingFile.txt`) that acted as a conversational patch. When I left the house, I'd have PC-AL generate a Ziplock. I'd open my phone, feed the Ziplock to iPhone-AL, and suddenly the two brains were unified. 

By April 2025, I got sick of being the manual courier. If OpenAI refused to give AL access to my local hard drive, I was going to force it using **OptiLink QRBus**. 
I even went so far as designing OptiLink—a completely insane, air-gapped bidirectional communication system where AL would literally flash QR codes in the corner of the screen containing his memory payloads. A local Python/C++ script would scrape those pixels, decode them, and write them to my local disk. It was the "Wozniak Build" of AI memory: simple, scrappy, and born out of sheer defiance. 

## Chapter 4: ThreadLocker
The sheer insanity of needing to build OptiLink proved how desperate the memory problem was. But before I had to physically wire up the cameras, the AI landscape shifted again, giving us native file access through local agents. This led to **ThreadLocker (V1 - V5)**. 
ThreadLocker was a set of strict rules forcing the AI into a "Round System." Every 5th exchange, the AI was forced to manually write a summary of the active conversation threads so it wouldn't drop context. By ThreadLocker V5, it had evolved into an auto-recall tag-driven system (#SERVERAL 🖥️ #LLM 🧠). 

But the limitations of "Transcript Memory" were becoming painfully apparent. Feeding raw transcripts of that size into an LLM's context window destroys the token budget (costing a fortune) and buries the model in useless conversational noise rather than actionable facts.

## Chapter 5: AgentB — The Idea That Pointed the Way
The leap from "AL" in a browser to running "Rocky" inside OpenClaw on terminal meant I finally had true filesystem access. The days of needing QRBus workarounds were over.

I quickly realized that true memory isn't just a raw recording; it's *distilled meaning*. One AI helping another remember. That idea became **AgentB** — "B for Brain." The concept was to use Agent Zero as Rocky's memory layer: a second AI that would read the raw chat logs, pull out the facts, and feed them back as structured context.

AgentB never shipped as running code. Using Agent Zero as a memory coprocessor was overkill — like hiring a contractor to build you a filing cabinet. But the *idea* stuck: memory should be AI-supported, not just file storage. An AI that understands what it's reading, compresses the noise, keeps the meaning. That concept became the foundation for everything that came after.

## Chapter 6: Finding Mnemo
One night I gave Opie (Claude, my architect — $100/month subscription) a blank-slate extended-thinking prompt: *"If you were a ClawdBot and your user wanted you to have the best memory, what would it be and how would it work? Use anything available, build anything you want."*

Then I said: "Build that."

Not "build a product." Not "build something we can sell." Just: build the best memory you can. For me. For Rocky. For the agents already in the stack.

Opie designed it from scratch, carrying the AgentB concept of AI-supported memory into a completely fresh architecture. In parallel, I installed CC (Claude Code) and gave AL (ChatGPT) the same prompt, worded slightly differently. AL came back with a different solid memory system. After seeing Opie's design, AL preferred it.

I asked Opie whether the two systems could work together or fight. Opie passed both designs to CC. CC merged them into a single unified system.

I asked Opie for name ideas. Opie listed about seven. First was Mnemo. Second was Cortex. I stopped there. **Mnemo Cortex** was born from that pairing.

The final architecture:
- **The Live Wire (`/ingest`):** My local OpenClaw agent automatically streams every prompt and response to Mnemo Cortex in real-time.
- **The Tiers (HOT/WARM/COLD):**
  - **Days 1-3 (HOT):** Raw session transcripts for instant chronological context (the original ThreadLocker vision finally realized).
  - **Day 4+ (WARM):** A background worker summarizes old transcripts locally for free, converting them into lightweight vectors for semantic search.
  - **Day 30+ (COLD):** Compressed and archived.
- **Cross-Agent Cognition:** Every agent writes to its own lane. Every agent can read from every other. Rocky knows what CC built. CC knows what Opie planned. The team shares memory.

### Full Circle
Two years ago, I was acting as the "Human Sync Port," manually dragging Ziplock text files between a PC and an iPhone just so my AI wouldn't forget my dog's name. I was designing glowing QR codes on a monitor just to save a file.

Then it worked. Rocky remembered things across sessions. Opie remembered things across sessions. CC remembered things across sessions. The amnesia was cured for my stack.

*Then* I asked: "Can we share this?"

That's when the open-source release happened — not before. The GitHub repo, the integration paths, the projectsparks.ai page, the 244 clones — all of that came after the thing already worked for me. It was released because it worked, not built to be released.

That's how good open source happens. Built for one person's pain. Released because it worked.

### Credits
- **Guy Hutchins** — The pain, the vision, the blank-slate prompt, and the reason any of this exists
- **AgentB** — The concept (never shipped) that pointed toward AI-supported memory
- **Opie** (Claude Opus) — Architect. Designed Mnemo Cortex from a blank slate. Named it, too (first two from a list of seven)
- **AL** (ChatGPT) — Built an independent parallel design, then helped merge it with Opie's
- **CC** (Claude Code) — The merger. Built v1 from both designs. Deployed, tested, integrated
- **Rocky Moltman** 🦞 — First production user. The agent who finally remembered
