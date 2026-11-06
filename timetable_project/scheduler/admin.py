from django.contrib import admin, messages
from django.utils.html import format_html
from .models import (
    Semester,
    YearDivision,
    Subject,
    Room,
    TimeSlot,
    Teacher,
    TeacherUnavailability,
    Assignment,
    TimetableEntry,
    SchedulingIssue,
)
from .solver import generate_timetable, TimetableSolverError


@admin.register(Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "get_number_of_weeks", "generate_link")
    actions = ["action_regenerate"]

    def get_number_of_weeks(self, obj):
        return obj.number_of_weeks()
    get_number_of_weeks.short_description = "Weeks"

    def generate_link(self, obj):
        return format_html(
            '<a class="button" style="background:#2563eb;color:white;padding:4px 10px;border-radius:4px;text-decoration:none;" '
            'href="/generate/?semester_id={}">Generate Timetable</a>',
            obj.id
        )
    generate_link.short_description = "Action"

    @admin.action(description="Generate / Regenerate Timetable for selected semester(s)")
    def action_regenerate(self, request, queryset):
        for sem in queryset:
            try:
                res = generate_timetable(sem.id)
                self.message_user(
                    request,
                    f"Generated for '{sem.name}': {res['total_hours_scheduled']}/{res['total_hours_requested']} hours scheduled ({res['issues_count']} issues).",
                    messages.SUCCESS
                )
            except TimetableSolverError as e:
                self.message_user(request, f"Error generating for '{sem.name}': {str(e)}", messages.ERROR)


@admin.register(YearDivision)
class YearDivisionAdmin(admin.ModelAdmin):
    list_display = ("name", "year", "division_number", "strength")
    list_filter = ("year",)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_lab")
    list_filter = ("is_lab",)
    search_fields = ("name", "code")


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "is_lab", "capacity")
    list_filter = ("is_lab",)
    search_fields = ("name",)


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("day", "period_number", "start_time", "end_time")
    list_filter = ("day",)


class TeacherUnavailabilityInline(admin.TabularInline):
    model = TeacherUnavailability
    extra = 1


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ("name", "max_hours_per_week")
    search_fields = ("name",)
    inlines = [TeacherUnavailabilityInline]


@admin.register(TeacherUnavailability)
class TeacherUnavailabilityAdmin(admin.ModelAdmin):
    list_display = ("teacher", "time_slot")
    list_filter = ("teacher", "time_slot__day")


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("division", "subject", "teacher", "semester", "total_hours_for_semester", "get_weekly_hours", "regenerate_btn")
    list_filter = ("semester", "division__year", "teacher", "subject")
    search_fields = ("teacher__name", "subject__name", "division__name")
    actions = ["action_regenerate_semester"]

    def get_weekly_hours(self, obj):
        return f"{obj.weekly_hours()} h/wk"
    get_weekly_hours.short_description = "Weekly Hours"

    def regenerate_btn(self, obj):
        return format_html(
            '<a class="button" style="background:#059669;color:white;padding:3px 8px;border-radius:4px;text-decoration:none;" '
            'href="/generate/?semester_id={}">Regenerate</a>',
            obj.semester_id
        )
    regenerate_btn.short_description = "Regenerate"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Inform the admin and provide direct link to regenerate
        regen_url = f"/generate/?semester_id={obj.semester_id}"
        messages.info(
            request,
            format_html(
                'Assignment updated. <a href="{}" style="font-weight:bold;color:#1d4ed8;text-decoration:underline;">Click here to Regenerate Timetable</a> for {} to apply changes and check for issues.',
                regen_url,
                obj.semester.name
            )
        )

    @admin.action(description="Regenerate Timetable for selected assignments' semester")
    def action_regenerate_semester(self, request, queryset):
        semester_ids = set(queryset.values_list("semester_id", flat=True))
        for s_id in semester_ids:
            try:
                res = generate_timetable(s_id)
                self.message_user(request, res["message"], messages.SUCCESS)
            except TimetableSolverError as e:
                self.message_user(request, f"Solver error: {str(e)}", messages.ERROR)


@admin.register(TimetableEntry)
class TimetableEntryAdmin(admin.ModelAdmin):
    list_display = (
        "semester", "time_slot", "get_division", "get_subject",
        "get_teacher", "room", "pinned_badge"
    )
    list_filter = ("semester", "time_slot__day", "room", "is_pinned")
    search_fields = ("assignment__teacher__name", "assignment__subject__name", "assignment__division__name")
    fields = ("semester", "assignment", "room", "time_slot", "is_pinned")

    def get_division(self, obj):
        return obj.assignment.division.name
    get_division.short_description = "Division"

    def get_subject(self, obj):
        return obj.assignment.subject.name
    get_subject.short_description = "Subject"

    def get_teacher(self, obj):
        return obj.assignment.teacher.name
    get_teacher.short_description = "Teacher"

    def pinned_badge(self, obj):
        if obj.is_pinned:
            return format_html(
                '<span style="background:#f59e0b;color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:bold;">📌 PINNED</span>'
            )
        return format_html(
            '<span style="background:#e2e8f0;color:#64748b;padding:2px 8px;border-radius:12px;font-size:11px;">Auto</span>'
        )
    pinned_badge.short_description = "Type"

    def save_model(self, request, obj, form, change):
        # Auto-mark as pinned when manually added/saved through admin
        obj.is_pinned = True
        super().save_model(request, obj, form, change)

        # ---------------------------------------------------------------
        # Conflict Detection — check all 3 types of conflicts
        # ---------------------------------------------------------------
        conflicts = []

        # 1. Room Conflict — same room booked at same time
        room_conflicts = TimetableEntry.objects.filter(
            semester=obj.semester,
            time_slot=obj.time_slot,
            room=obj.room
        ).exclude(pk=obj.pk).select_related(
            "assignment__division", "assignment__subject", "assignment__teacher"
        )
        for c in room_conflicts:
            conflicts.append({
                "type": "Room",
                "icon": "🏠",
                "color": "#dc2626",
                "msg": (
                    f"Room '{obj.room.name}' is already booked at {obj.time_slot} "
                    f"for {c.assignment.division.name} — {c.assignment.subject.name} "
                    f"(by {c.assignment.teacher.name})"
                )
            })

        # 2. Teacher Conflict — same teacher teaching two classes at same time
        teacher_conflicts = TimetableEntry.objects.filter(
            semester=obj.semester,
            time_slot=obj.time_slot,
            assignment__teacher=obj.assignment.teacher
        ).exclude(pk=obj.pk).select_related(
            "assignment__division", "assignment__subject"
        )
        for c in teacher_conflicts:
            conflicts.append({
                "type": "Teacher",
                "icon": "👨‍🏫",
                "color": "#7c3aed",
                "msg": (
                    f"Teacher '{obj.assignment.teacher.name}' is already teaching "
                    f"{c.assignment.subject.name} to {c.assignment.division.name} "
                    f"at {obj.time_slot}"
                )
            })

        # 3. Division Conflict — same division has two subjects at same time
        division_conflicts = TimetableEntry.objects.filter(
            semester=obj.semester,
            time_slot=obj.time_slot,
            assignment__division=obj.assignment.division
        ).exclude(pk=obj.pk).select_related(
            "assignment__subject", "assignment__teacher"
        )
        for c in division_conflicts:
            conflicts.append({
                "type": "Division",
                "icon": "👥",
                "color": "#b45309",
                "msg": (
                    f"Division '{obj.assignment.division.name}' already has "
                    f"{c.assignment.subject.name} (by {c.assignment.teacher.name}) "
                    f"at {obj.time_slot}"
                )
            })

        # Show warnings for each conflict
        if conflicts:
            for cf in conflicts:
                messages.warning(
                    request,
                    format_html(
                        '<strong>{} {} Conflict Detected:</strong> {}',
                        cf["icon"], cf["type"], cf["msg"]
                    )
                )
            # Prompt to regenerate to auto-resolve
            regen_url = f"/generate/?semester_id={obj.semester.id}"
            messages.error(
                request,
                format_html(
                    '⚠️ <strong>{} conflict(s) found!</strong> This entry has been saved as 📌 Pinned. '
                    '<a href="{}" style="font-weight:bold;color:#1d4ed8;text-decoration:underline;">'
                    '👉 Click here to Regenerate Timetable</a> — the solver will auto-resolve all conflicts '
                    'while keeping your pinned entry fixed.',
                    len(conflicts), regen_url
                )
            )
        else:
            # No conflicts — all clear
            regen_url = f"/generate/?semester_id={obj.semester.id}"
            messages.success(
                request,
                format_html(
                    '✅ <strong>No conflicts!</strong> Entry saved as 📌 Pinned — it will be preserved during regeneration. '
                    '<a href="{}" style="font-weight:bold;color:#1d4ed8;text-decoration:underline;">Regenerate Timetable</a> '
                    'to fill remaining slots around this fixed entry.',
                    regen_url
                )
            )


@admin.register(SchedulingIssue)
class SchedulingIssueAdmin(admin.ModelAdmin):
    list_display = ("semester", "assignment", "hours_requested", "hours_scheduled", "reason", "suggestion")
    list_filter = ("semester",)
    search_fields = ("assignment__teacher__name", "assignment__subject__name", "reason", "suggestion")
