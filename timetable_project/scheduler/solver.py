from collections import defaultdict
from django.db import transaction
from ortools.sat.python import cp_model

from .models import (
    Semester,
    Assignment,
    Room,
    TimeSlot,
    TeacherUnavailability,
    TimetableEntry,
    SchedulingIssue,
)


class TimetableSolverError(Exception):
    """Custom exception raised when timetable scheduling fails or data is missing."""
    pass


def generate_timetable(semester_id: int) -> dict:
    """
    Generates a conflict-free timetable for the given semester using Google OR-Tools CP-SAT.

    Pinned entries (is_pinned=True) are manually fixed lectures that are preserved across
    regeneration. The solver treats pinned slots as already occupied and fills remaining
    hours around them without touching them.

    Hard constraints:
      1. Teacher cannot be in two places at the same time slot.
      2. Division cannot have two subjects in the same time slot.
      3. Room cannot hold two divisions in the same time slot.
      4. Lab subjects only in lab rooms; theory subjects only in non-lab rooms.
      5. Teacher is never scheduled during their TeacherUnavailability time slots.
      6. Scheduling only happens within existing TimeSlots.
      7. For each assignment, solver_scheduled_hours <= remaining_weekly_hours
         (after subtracting already-pinned hours).

    Soft objective:
      Maximize total scheduled hours across all assignments.

    After solving:
      - Clears only NON-PINNED TimetableEntry rows for this semester.
      - Pinned entries are NEVER deleted or modified.
      - Saves new solver TimetableEntry rows (is_pinned=False).
      - Creates SchedulingIssue rows for any assignment where total hours < weekly_hours().
    """
    try:
        semester = Semester.objects.get(pk=semester_id)
    except Semester.DoesNotExist:
        raise TimetableSolverError(f"Semester with id {semester_id} does not exist.")

    assignments = list(
        Assignment.objects.filter(semester=semester)
        .select_related("teacher", "subject", "division")
    )

    if not assignments:
        return {
            "success": True,
            "total_hours_scheduled": 0,
            "total_hours_requested": 0,
            "issues_count": 0,
            "message": "No assignments found for this semester."
        }

    rooms = list(Room.objects.all())
    time_slots = list(TimeSlot.objects.all().order_by("day", "period_number"))

    # Pre-checks for missing resources
    if not time_slots:
        raise TimetableSolverError("No time slots available in database. Generate time slots first.")

    regular_rooms = [r for r in rooms if not r.is_lab]
    lab_rooms = [r for r in rooms if r.is_lab]

    for a in assignments:
        if a.subject.is_lab and not lab_rooms:
            raise TimetableSolverError(
                f"No lab room exists for '{a.subject.name}' -- add at least one Room with is_lab=True."
            )
        if not a.subject.is_lab and not regular_rooms:
            raise TimetableSolverError(
                f"No regular room exists for '{a.subject.name}' -- add at least one Room with is_lab=False."
            )

    # -------------------------------------------------------------------------
    # Load Pinned Entries — these are FIXED and must not be touched by the solver
    # -------------------------------------------------------------------------
    pinned_entries = list(
        TimetableEntry.objects.filter(semester=semester, is_pinned=True)
        .select_related(
            'assignment__teacher', 'assignment__subject',
            'assignment__division', 'room', 'time_slot'
        )
    )

    # Build "occupied" sets from pinned entries so solver avoids those slots
    pinned_teacher_slots = set()   # (teacher_id, slot_id)
    pinned_division_slots = set()  # (division_id, slot_id)
    pinned_room_slots = set()      # (room_id, slot_id)
    pinned_hours_per_assignment = defaultdict(int)  # assignment_id -> count

    for pe in pinned_entries:
        t_id = pe.assignment.teacher.id
        div_id = pe.assignment.division.id
        r_id = pe.room.id
        slot_id = pe.time_slot.id
        a_id = pe.assignment.id

        pinned_teacher_slots.add((t_id, slot_id))
        pinned_division_slots.add((div_id, slot_id))
        pinned_room_slots.add((r_id, slot_id))
        pinned_hours_per_assignment[a_id] += 1

    # Teacher unavailabilities map: teacher_id -> set of time_slot_ids
    unavailability_records = TeacherUnavailability.objects.all().values_list("teacher_id", "time_slot_id")
    teacher_unavail = defaultdict(set)
    for t_id, slot_id in unavailability_records:
        teacher_unavail[t_id].add(slot_id)

    # -------------------------------------------------------------------------
    # CP-SAT Model Formulation
    # -------------------------------------------------------------------------
    model = cp_model.CpModel()

    # Decision variables: x[a_id, r_id, slot_id] in {0, 1}
    # Only create variable if:
    # 1. room type matches subject type (lab vs theory)
    # 2. teacher is available in that slot
    # 3. slot is NOT already occupied by a pinned entry for this teacher/division/room
    x = {}

    # Index structures for fast constraint posting
    teacher_slot_vars = defaultdict(list)    # (teacher_id, slot_id) -> list of vars
    division_slot_vars = defaultdict(list)   # (division_id, slot_id) -> list of vars
    room_slot_vars = defaultdict(list)       # (room_id, slot_id) -> list of vars
    assignment_vars = defaultdict(list)      # a_id -> list of vars
    teacher_all_vars = defaultdict(list)     # teacher_id -> list of vars

    for a in assignments:
        matching_rooms = lab_rooms if a.subject.is_lab else regular_rooms
        t_id = a.teacher.id
        div_id = a.division.id

        for slot in time_slots:
            slot_id = slot.id

            # Skip: teacher unavailable
            if slot_id in teacher_unavail[t_id]:
                continue

            # Skip: slot already occupied by a pinned entry for this teacher
            if (t_id, slot_id) in pinned_teacher_slots:
                continue

            # Skip: slot already occupied by a pinned entry for this division
            if (div_id, slot_id) in pinned_division_slots:
                continue

            for r in matching_rooms:
                r_id = r.id

                # Skip: room already occupied by a pinned entry
                if (r_id, slot_id) in pinned_room_slots:
                    continue

                var_name = f"x_{a.id}_{r_id}_{slot_id}"
                var = model.NewBoolVar(var_name)

                x[a.id, r_id, slot_id] = var
                assignment_vars[a.id].append(var)
                teacher_slot_vars[t_id, slot_id].append(var)
                division_slot_vars[div_id, slot_id].append(var)
                room_slot_vars[r_id, slot_id].append(var)
                teacher_all_vars[t_id].append(var)

    # Constraint 1: Teacher cannot be in two places at the same time slot
    for (t_id, slot_id), var_list in teacher_slot_vars.items():
        model.Add(sum(var_list) <= 1)

    # Constraint 2: Division cannot have two subjects at the same time slot
    for (div_id, slot_id), var_list in division_slot_vars.items():
        model.Add(sum(var_list) <= 1)

    # Constraint 3: Room cannot hold two divisions at the same time slot
    for (r_id, slot_id), var_list in room_slot_vars.items():
        model.Add(sum(var_list) <= 1)

    # Constraint 7: Solver scheduled hours <= remaining weekly hours for each assignment
    # (remaining = weekly_hours() - pinned_hours already in DB)
    total_requested_hours = 0
    for a in assignments:
        req_hours = a.weekly_hours()
        total_requested_hours += req_hours
        already_pinned = pinned_hours_per_assignment[a.id]
        remaining_needed = max(0, req_hours - already_pinned)
        if a.id in assignment_vars:
            model.Add(sum(assignment_vars[a.id]) <= remaining_needed)

    # Additional constraint: Teacher max hours per week
    # (pinned hours count toward the max too)
    for t_id, var_list in teacher_all_vars.items():
        teacher_obj = next((a.teacher for a in assignments if a.teacher.id == t_id), None)
        if teacher_obj and teacher_obj.max_hours_per_week:
            # Count how many pinned hours this teacher already has
            pinned_teacher_hours = sum(
                1 for pe in pinned_entries if pe.assignment.teacher.id == t_id
            )
            remaining_max = max(0, teacher_obj.max_hours_per_week - pinned_teacher_hours)
            model.Add(sum(var_list) <= remaining_max)

    # Soft Objective: Maximize total scheduled hours across all assignments
    all_vars = list(x.values())
    if all_vars:
        model.Maximize(sum(all_vars))

    # Solve
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    solver.parameters.num_workers = 4

    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise TimetableSolverError(
            "Solver could not find a feasible schedule. "
            "Please check if room constraints or working hours are severely under-provisioned."
        )

    # -------------------------------------------------------------------------
    # Persist Results
    # -------------------------------------------------------------------------
    scheduled_entries = []
    assignment_scheduled_count = defaultdict(int)

    # Pre-fetch lookup dictionaries
    room_dict = {r.id: r for r in rooms}
    slot_dict = {s.id: s for s in time_slots}
    assignment_dict = {a.id: a for a in assignments}

    for (a_id, r_id, slot_id), var in x.items():
        if solver.Value(var) == 1:
            scheduled_entries.append(
                TimetableEntry(
                    semester=semester,
                    assignment=assignment_dict[a_id],
                    room=room_dict[r_id],
                    time_slot=slot_dict[slot_id],
                    is_pinned=False,  # Solver-generated entries are never pinned
                )
            )
            assignment_scheduled_count[a_id] += 1

    # Total scheduled = solver entries + pinned entries (pinned count toward hours)
    total_solver_hours = len(scheduled_entries)
    total_pinned_hours = len(pinned_entries)
    total_scheduled_hours = total_solver_hours + total_pinned_hours

    scheduling_issues = []

    # Include pinned hours in per-assignment scheduled count for issue detection
    for a_id, count in pinned_hours_per_assignment.items():
        assignment_scheduled_count[a_id] += count

    # Calculate division-level and teacher-level totals for intelligent diagnostics
    div_requested_totals = defaultdict(int)
    teacher_requested_totals = defaultdict(int)
    for a in assignments:
        div_requested_totals[a.division.id] += a.weekly_hours()
        teacher_requested_totals[a.teacher.id] += a.weekly_hours()

    total_slots_count = len(time_slots)

    for a in assignments:
        requested = a.weekly_hours()
        scheduled = assignment_scheduled_count[a.id]

        if scheduled < requested:
            # Construct meaningful, specific reason and actionable suggestion
            reasons = []
            suggestions = []

            t = a.teacher
            div = a.division
            unavail_count = len(teacher_unavail[t.id])

            if teacher_requested_totals[t.id] > t.max_hours_per_week:
                reasons.append(
                    f"{t.name}'s total assigned workload ({teacher_requested_totals[t.id]}h/wk) "
                    f"exceeds maximum allowed limit of {t.max_hours_per_week}h/wk."
                )
                suggestions.append(
                    f"Increase {t.name}'s max hours per week or reassign {a.division.name} "
                    f"to another faculty member."
                )
            elif unavail_count > 0:
                reasons.append(
                    f"{t.name} has {unavail_count} unavailable slot(s) limiting conflict-free placement."
                )
                suggestions.append(
                    f"Consider relaxing unavailability restrictions for {t.name} to unlock available periods."
                )

            if div_requested_totals[div.id] > total_slots_count:
                reasons.append(
                    f"{div.name} has {div_requested_totals[div.id]} total requested hours, "
                    f"exceeding the total {total_slots_count} available time slots in the week."
                )
                suggestions.append(
                    f"Reduce curriculum hours for {div.name} or configure additional daily periods/working days."
                )

            if not reasons:
                reasons.append(
                    f"{t.name}'s other assigned divisions occupy all available conflict-free slots this week."
                )
                suggestions.append(
                    f"Consider adding more time slots or reducing weekly hours for {a.subject.name} "
                    f"in {div.name} by {requested - scheduled} hour(s)/week."
                )

            scheduling_issues.append(
                SchedulingIssue(
                    semester=semester,
                    assignment=a,
                    hours_requested=requested,
                    hours_scheduled=scheduled,
                    reason=" ".join(reasons),
                    suggestion=" ".join(suggestions),
                )
            )

    with transaction.atomic():
        # Only delete NON-PINNED entries — pinned entries are preserved!
        TimetableEntry.objects.filter(semester=semester, is_pinned=False).delete()
        SchedulingIssue.objects.filter(semester=semester).delete()

        # Bulk create new solver-generated entries and issues
        TimetableEntry.objects.bulk_create(scheduled_entries)
        SchedulingIssue.objects.bulk_create(scheduling_issues)

    pinned_count = len(pinned_entries)
    return {
        "success": True,
        "total_hours_scheduled": total_scheduled_hours,
        "total_hours_requested": total_requested_hours,
        "pinned_count": pinned_count,
        "issues_count": len(scheduling_issues),
        "message": (
            f"Successfully generated timetable with {total_scheduled_hours}/{total_requested_hours} "
            f"hours scheduled ({pinned_count} pinned + {total_solver_hours} auto-scheduled, "
            f"{len(scheduling_issues)} issues reported)."
        )
    }
