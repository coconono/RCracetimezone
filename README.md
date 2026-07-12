# RC Timing Zone

I need a timing system for RC races. The plan is to make it portable and extensible.

## RC time trial Lap Timer

arduino code cobbled together and beaten on by robots. To track lap times.

### Features

- LED output
- serial text interface
- set up a race(nnumber of laps), watch the lights count down, it'll output time elapsed on each lap
- on the final lap it will output the total time

### Folder Structure

- `src/` — Arduino sketches and main code
- `lib/` — Custom libraries (if needed)
- `.github/` — Copilot and workflow instructions

### Getting Started

- I built this in TinkerCad and then fed it to copilot for further improvements. Completely untested.

### Requirements

- Arduino board (Uno, Nano, etc.)
- some type of IR trip sensor

### Usage

- push code to arduino
- follow prompts that show up on the serial interface
- when the lights count down, its time to race

## Tools

As I need software tools I'll stick them here.

The first thing I made was Track_sim because I'd had this idea of simulating race traffic and timing it. Rather spinning it off into its own thing like a zillion other things I'm working on (cough cough Hole Wizards), it made more sense to put this here.

After I remember I need a timing system again, I'll look into figuring out how I can simulate the arduino code execution as part of a Track_sim race.

### Track_sim

python programs to build cars, tracks and race them. Meant to test the timing system logic. Turned into a UI and AI testbed. Has gotten out of hand. See its README.md for more instructions.

## Things left to do

- error conditions and handling need lots of improvement
- integrate timing into track_sim
- IRL implementation and testing
