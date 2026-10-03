namespace RetryGuard

inductive Outcome where
  | success
  | transient
  | timeout
deriving DecidableEq, Repr

inductive Status where
  | running
  | gaveUp
  | succeeded
deriving DecidableEq, Repr

structure State where
  errors : Nat
  timeouts : Nat
  attempts : Nat
  status : Status
deriving DecidableEq, Repr

structure Cfg where
  maxErrors : Nat
  maxTimeouts : Nat
  fixed : Bool

def Cfg.maxAttempts (cfg : Cfg) : Nat := cfg.maxErrors + cfg.maxTimeouts

def State.init : State := { errors := 0, timeouts := 0, attempts := 0, status := .running }

def State.done (s : State) : Bool :=
  match s.status with
  | .running => false
  | _ => true

def giveUpCond (cfg : Cfg) (e t a : Nat) : Bool :=
  e ≥ cfg.maxErrors || t ≥ cfg.maxTimeouts || (cfg.fixed && a ≥ cfg.maxAttempts)

def step (cfg : Cfg) (s : State) (o : Outcome) : State :=
  if s.done then s
  else
    match o with
    | .success =>
      { s with attempts := s.attempts + 1, status := .succeeded }
    | .transient =>
      let e := s.errors + 1
      let t := 0
      let a := s.attempts + 1
      { errors := e, timeouts := t, attempts := a,
        status := if giveUpCond cfg e t a then .gaveUp else .running }
    | .timeout =>
      let e := 0
      let t := s.timeouts + 1
      let a := s.attempts + 1
      { errors := e, timeouts := t, attempts := a,
        status := if giveUpCond cfg e t a then .gaveUp else .running }

def run (cfg : Cfg) : State → List Outcome → State
  | s, [] => s
  | s, o :: os => run cfg (step cfg s o) os

def defaultCfg : Cfg := { maxErrors := 3, maxTimeouts := 3, fixed := true }
def buggyCfg : Cfg := { defaultCfg with fixed := false }
def smallCfg : Cfg := { maxErrors := 2, maxTimeouts := 2, fixed := true }

theorem run_sticky (cfg : Cfg) (s : State) (os : List Outcome) (h : s.done = true) :
    run cfg s os = s := by
  induction os generalizing s with
  | nil => rfl
  | cons o os ih =>
    have hstep : step cfg s o = s := by
      unfold step
      simp [h]
    unfold run
    rw [hstep]
    exact ih s h

theorem step_not_done_attempts_lt (cfg : Cfg) (hf : cfg.fixed = true) (s : State) (o : Outcome)
    (hnd : s.done = false) (hnd' : (step cfg s o).done = false) :
    (step cfg s o).attempts < cfg.maxAttempts := by
  unfold step at hnd' ⊢
  simp only [hnd, if_neg, Bool.false_eq_true, not_false_eq_true] at hnd' ⊢
  cases o with
  | success => simp [State.done] at hnd'
  | transient =>
    simp only [State.done, giveUpCond, hf] at hnd'
    simp only [State.done]
    revert hnd'
    by_cases h1 : s.errors + 1 ≥ cfg.maxErrors <;>
    by_cases h2 : (0 : Nat) ≥ cfg.maxTimeouts <;>
    by_cases h3 : s.attempts + 1 ≥ cfg.maxAttempts <;>
      simp_all <;> omega
  | timeout =>
    simp only [State.done, giveUpCond, hf] at hnd'
    simp only [State.done]
    revert hnd'
    by_cases h1 : (0 : Nat) ≥ cfg.maxErrors <;>
    by_cases h2 : s.timeouts + 1 ≥ cfg.maxTimeouts <;>
    by_cases h3 : s.attempts + 1 ≥ cfg.maxAttempts <;>
      simp_all <;> omega

theorem bounded_termination_from (cfg : Cfg) (hf : cfg.fixed = true) :
    ∀ (os : List Outcome) (s : State),
      (s.done = false → s.attempts < cfg.maxAttempts) →
      cfg.maxAttempts ≤ s.attempts + os.length →
      (run cfg s os).done = true := by
  intro os
  induction os with
  | nil =>
    intro s hinv hle
    simp only [run]
    cases hb : s.done with
    | true => rfl
    | false =>
      have := hinv hb
      simp only [List.length_nil, Nat.add_zero] at hle
      omega
  | cons o os ih =>
    intro s hinv hle
    by_cases hs : s.done = true
    · rw [run_sticky cfg s (o :: os) hs]
      exact hs
    · simp only [Bool.not_eq_true] at hs
      unfold run
      by_cases hs'done : (step cfg s o).done = true
      · rw [run_sticky cfg (step cfg s o) os hs'done]
        exact hs'done
      · simp only [Bool.not_eq_true] at hs'done
        have hattempts_lt : (step cfg s o).attempts < cfg.maxAttempts :=
          step_not_done_attempts_lt cfg hf s o hs hs'done
        have hattempts_eq : (step cfg s o).attempts = s.attempts + 1 := by
          unfold step
          simp only [hs, if_neg, Bool.false_eq_true, not_false_eq_true]
          cases o <;> simp
        apply ih (step cfg s o) (fun _ => hattempts_lt)
        simp only [List.length_cons] at hle
        omega

theorem fixed_terminates (os : List Outcome) (h : os.length ≥ defaultCfg.maxAttempts) :
    (run defaultCfg State.init os).done = true := by
  apply bounded_termination_from defaultCfg rfl os State.init
  · intro _
    decide
  · simpa [State.init] using h

theorem fixed_terminates_small (os : List Outcome) (h : os.length ≥ smallCfg.maxAttempts) :
    (run smallCfg State.init os).done = true := by
  apply bounded_termination_from smallCfg rfl os State.init
  · intro _
    decide
  · simpa [State.init] using h

def altList : List Outcome :=
  [Outcome.transient, Outcome.timeout, Outcome.transient, Outcome.timeout, Outcome.transient,
   Outcome.timeout]

theorem buggy_not_bounded :
    ¬ (∀ os : List Outcome, os.length ≥ buggyCfg.maxAttempts →
        (run buggyCfg State.init os).done = true) := by
  intro h
  have hlen : altList.length ≥ buggyCfg.maxAttempts := by decide
  have hdone := h altList hlen
  have hnotdone : (run buggyCfg State.init altList).done = false := by decide
  rw [hnotdone] at hdone
  exact absurd hdone (by decide)

end RetryGuard
