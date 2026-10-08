# Virtual Lab

The second game in the Arena (`/student/arena/lab` on the frontend). A chemistry bench where
students drag equipment, pour chemicals, heat, test gases and run guided experiments.

## Where things live

| Part | Where | Notes |
|---|---|---|
| Chemistry engine | frontend `src/pages/student/lab/engine/` | Runs in the browser. `substances.ts` (91 substances, ~57 cabinet reagents), `sim.ts` (reactions, dissolving, neutralisation, heat, boiling, settling, colour, pH, gas and flame tests), `experiments.ts` (12 guided experiments and the step checker). |
| Bench UI | frontend `src/pages/student/lab/` | Glassware, burner, tools, particles, cabinet, notebook, guided mode. |
| Teacher page | frontend `src/pages/arena-manage/lab-experiments.tsx` | `/teacher/arena/lab`, `/admin/arena/lab`. |
| Assignments and results | backend `app/services/lab.py`, `app/api/v1/lab.py` | Collections `lab_assignments`, `lab_attempts`. |

## Chemistry model

* A vessel holds millilitres of water (`h2o`) and millimoles of everything else. A bottle of
  "1 M hydrochloric acid" poured 10 mL adds 10 mL of water and 10 mmol of HCl; solids are
  weighed in grams and converted by molar mass.
* Reactions are written on **ions** where chemistry works that way (`@Ag+` + `@Cl-` → AgCl),
  so silver nitrate meets any chloride, barium chloride any sulfate, any acid any carbonate.
* Rates follow the concentration of what is dissolved (never the size of a lump) and speed up
  with temperature. Heat changes are calibrated to classroom values (1 M acid + alkali ≈ +7 °C,
  2 g NaOH in 50 mL ≈ +10 °C, NH₄Cl ≈ −6 °C).
* Combinations without a rule just mix: "no visible reaction" is a real answer (copper and
  dilute acid), not a gap filled with invented chemistry.
* Tested by a scripted bench of 23 classic experiments (neutralisation, indicators, metals and
  acids, CO₂/limewater via a delivery tube, six precipitates, displacement, catalysts, starch,
  energy changes, thermal decomposition, sodium in water, flame tests, boiling, thiosulfate,
  ammonia) — all pass.

## Assignments

* A teacher sets a **library** experiment (by id; its content lives with the engine) or their
  **own** experiment (stored whole, same shape as the library's) for a class, with optional
  instructions and due date. Teachers reach the classes they teach or lead; admins all.
* Students see their class's active assignments in the lab's Experiments mode.
* `POST /lab/attempts` records a finished run (score, observations). The **first** completion of
  each experiment (or assignment) pays arena XP (30–70) and coins (15–40) by quiz score and
  counts toward the class leaderboard; repeats are recorded but pay nothing. Badges: First
  Experiment (1), Lab Scientist (5), Master Chemist (12).
* Teachers see every student's attempts, best score and latest observations.
