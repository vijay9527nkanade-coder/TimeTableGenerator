import math
from datetime import datetime, time, timedelta
from django.db import models


def ordinal(n: int) -> str:
    """Return ordinal representation of integer n (e.g. 1 -> '1st', 2 -> '2nd')."""
    if 11 <= (n % 100) <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


# ===========================================================================
# 1. Semester
# ===========================================================================
class Semester(models.Model):
    name = models.CharField(max_length=150)
    start_date = models.DateField()
    end_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.name} ({self.start_date} to {self.end_date})"

    def number_of_weeks(self) -> int:
        return max(1, (self.end_date - self.start_date).days // 7)


# ===========================================================================
# 2. YearDivision
# ===========================================================================
class YearDivision(models.Model):
    year = models.IntegerField(help_text="Year of study (1-4)")
    division_number = models.IntegerField(help_text="Sequential division number (1, 2, ...)")
    strength = models.IntegerField(default=60)
    name = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['year', 'division_number']
        unique_together = ('year', 'division_number')

    def save(self, *args, **kwargs):
        self.name = f"{ordinal(self.year)} Year - Division {self.division_number}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or f"{ordinal(self.year)} Year - Division {self.division_number}"

    @staticmethod
    def generate_for_year(year: int, number_of_divisions: int, strength: int):
        """
        Auto-creates that many YearDivision rows, numbered sequentially,
        continuing from however many already exist for that year.
        """
        existing_count = YearDivision.objects.filter(year=year).count()
        created_divisions = []
        for i in range(1, number_of_divisions + 1):
            div_num = existing_count + i
            division = YearDivision.objects.create(
                year=year,
                division_number=div_num,
                strength=strength
            )
            created_divisions.append(division)
        return created_divisions


# ===========================================================================
# 3. Subject
# ===========================================================================
class Subject(models.Model):
    code = models.CharField(max_length=50, blank=True, null=True)
    name = models.CharField(max_length=150)
    is_lab = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        type_str = "Lab" if self.is_lab else "Theory"
        if self.code:
            return f"{self.name} [{self.code}] ({type_str})"
        return f"{self.name} ({type_str})"


# ===========================================================================
# 4. Room
# ===========================================================================
class Room(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_lab = models.BooleanField(default=False)
    capacity = models.IntegerField(default=60)

    class Meta:
        ordering = ['is_lab', 'name']

    def __str__(self):
        room_type = "Lab" if self.is_lab else "Classroom"
        return f"{self.name} ({room_type}, Cap: {self.capacity})"

    @staticmethod
    def generate_rooms(number_of_regular_rooms: int, regular_room_capacity: int,
                       number_of_lab_rooms: int, lab_room_capacity: int):
        """
        Auto-creates regular and lab Room rows, named sequentially ('Room 1', 'Room 2'...)
        and ('Lab 1', 'Lab 2'...), continuing from however many already exist of each type.
        """
        created_rooms = []
        existing_regular = Room.objects.filter(is_lab=False).count()
        for i in range(1, number_of_regular_rooms + 1):
            room_name = f"Room {existing_regular + i}"
            room = Room.objects.create(
                name=room_name,
                is_lab=False,
                capacity=regular_room_capacity
            )
            created_rooms.append(room)

        existing_labs = Room.objects.filter(is_lab=True).count()
        for i in range(1, number_of_lab_rooms + 1):
            lab_name = f"Lab {existing_labs + i}"
            room = Room.objects.create(
                name=lab_name,
                is_lab=True,
                capacity=lab_room_capacity
            )
            created_rooms.append(room)

        return created_rooms


# ===========================================================================
# 5. TimeSlot
# ===========================================================================
DAY_CHOICES = [
    ('MON', 'Monday'),
    ('TUE', 'Tuesday'),
    ('WED', 'Wednesday'),
    ('THU', 'Thursday'),
    ('FRI', 'Friday'),
    ('SAT', 'Saturday'),
]

DAY_ORDER = {'MON': 1, 'TUE': 2, 'WED': 3, 'THU': 4, 'FRI': 5, 'SAT': 6}


class TimeSlot(models.Model):
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    period_number = models.IntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ['day', 'period_number']
        unique_together = ('day', 'period_number')

    def __str__(self):
        day_display = dict(DAY_CHOICES).get(self.day, self.day)
        return f"{day_display} Period {self.period_number} ({self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')})"

    @property
    def slot_code(self):
        return f"{self.day}-P{self.period_number}"


def _parse_time(t_val):
    if isinstance(t_val, time):
        return t_val
    if isinstance(t_val, str):
        parts = [int(p) for p in t_val.split(':')]
        return time(parts[0], parts[1])
    raise ValueError(f"Invalid time value: {t_val}")


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _minutes_to_time(m: int) -> time:
    return time(m // 60, m % 60)


def generate_time_slots(working_days, daily_start_time, daily_end_time,
                        period_duration_minutes, lunch_break):
    """
    Helper function that auto-creates all TimeSlot rows by dividing the daily
    start-end range into equal periods, for each working day.
    The lunch_break window (start_time, end_time) is completely excluded -- no
    period is generated that overlaps it at all. Periods are numbered sequentially
    across the whole day, skipping over the lunch gap.
    """
    start_t = _parse_time(daily_start_time)
    end_t = _parse_time(daily_end_time)
    start_mins = _time_to_minutes(start_t)
    end_mins = _time_to_minutes(end_t)

    lunch_start_mins = None
    lunch_end_mins = None
    if lunch_break:
        lunch_s = _parse_time(lunch_break.get('start_time'))
        lunch_e = _parse_time(lunch_break.get('end_time'))
        lunch_start_mins = _time_to_minutes(lunch_s)
        lunch_end_mins = _time_to_minutes(lunch_e)

    created_slots = []

    # Map full day names or abbreviations to choices key
    day_map = {
        'MON': 'MON', 'MONDAY': 'MON',
        'TUE': 'TUE', 'TUESDAY': 'TUE',
        'WED': 'WED', 'WEDNESDAY': 'WED',
        'THU': 'THU', 'THURSDAY': 'THU',
        'FRI': 'FRI', 'FRIDAY': 'FRI',
        'SAT': 'SAT', 'SATURDAY': 'SAT',
    }

    for raw_day in working_days:
        day_key = day_map.get(raw_day.upper().strip())
        if not day_key:
            continue

        curr = start_mins
        period_num = 1

        while curr + period_duration_minutes <= end_mins:
            p_start = curr
            p_end = curr + period_duration_minutes

            # Check if this period overlaps the lunch window
            if lunch_start_mins is not None and lunch_end_mins is not None:
                # Overlap condition: max(start1, start2) < min(end1, end2)
                if max(p_start, lunch_start_mins) < min(p_end, lunch_end_mins):
                    # Overlaps lunch. Skip forward to lunch_end
                    if curr < lunch_end_mins:
                        curr = lunch_end_mins
                    else:
                        curr += period_duration_minutes
                    continue

            # Non-overlapping period
            slot_start_time = _minutes_to_time(p_start)
            slot_end_time = _minutes_to_time(p_end)

            slot, created = TimeSlot.objects.get_or_create(
                day=day_key,
                period_number=period_num,
                defaults={
                    'start_time': slot_start_time,
                    'end_time': slot_end_time,
                }
            )
            created_slots.append(slot)
            period_num += 1
            curr = p_end

    return created_slots


# ===========================================================================
# 6. Teacher
# ===========================================================================
class Teacher(models.Model):
    name = models.CharField(max_length=150)
    max_hours_per_week = models.IntegerField(default=24)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} (Max: {self.max_hours_per_week}h/wk)"


# ===========================================================================
# 7. TeacherUnavailability
# ===========================================================================
class TeacherUnavailability(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='unavailabilities')
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='unavailable_teachers')

    class Meta:
        unique_together = ('teacher', 'time_slot')
        ordering = ['teacher', 'time_slot']

    def __str__(self):
        return f"{self.teacher.name} unavailable at {self.time_slot}"


# ===========================================================================
# 8. Assignment (the core scheduling unit)
# ===========================================================================
class Assignment(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='assignments')
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='assignments')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='assignments')
    division = models.ForeignKey(YearDivision, on_delete=models.CASCADE, related_name='assignments')
    total_hours_for_semester = models.IntegerField(default=60)

    class Meta:
        unique_together = ('semester', 'teacher', 'subject', 'division')
        ordering = ['division__year', 'division__division_number', 'subject__name']

    def __str__(self):
        return f"{self.division.name} - {self.subject.name} by {self.teacher.name} ({self.weekly_hours()}h/wk)"

    def weekly_hours(self) -> int:
        num_weeks = self.semester.number_of_weeks()
        return math.ceil(self.total_hours_for_semester / num_weeks)


# ===========================================================================
# 9. TimetableEntry (solver output + manually pinned entries)
# ===========================================================================
class TimetableEntry(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='timetable_entries')
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name='timetable_entries')
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='timetable_entries')
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='timetable_entries')
    is_pinned = models.BooleanField(
        default=False,
        help_text=(
            'Pinned entries are manually fixed and preserved during timetable regeneration. '
            'The solver schedules all other lectures around them.'
        )
    )

    class Meta:
        unique_together = ('semester', 'time_slot', 'room')
        ordering = ['time_slot__day', 'time_slot__period_number']

    def __str__(self):
        pin_tag = ' [PINNED]' if self.is_pinned else ''
        return (f"{self.time_slot} | {self.assignment.division.name} | "
                f"{self.assignment.subject.name} | {self.assignment.teacher.name} | {self.room.name}{pin_tag}")


# ===========================================================================
# 10. SchedulingIssue (for honest reporting when full hours can't be scheduled)
# ===========================================================================
class SchedulingIssue(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name='scheduling_issues')
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name='scheduling_issues')
    hours_requested = models.IntegerField()
    hours_scheduled = models.IntegerField()
    reason = models.TextField()
    suggestion = models.TextField()

    class Meta:
        ordering = ['assignment__division', 'assignment__subject']

    def __str__(self):
        return (f"Issue for {self.assignment}: {self.hours_scheduled}/{self.hours_requested} hrs scheduled")
