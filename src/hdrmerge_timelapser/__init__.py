"""Plan and produce an HDR timelapse from bracketed bursts.

    hdrmerge-timelapser detect FOLDER     measure, and write the plan
    hdrmerge-timelapser edit   FOLDER     look at the plan and correct it
    hdrmerge-timelapser apply  FOLDER     merge and develop what the plan says

Three commands and not one run because the separation is the design: measuring
is slow and fallible, merging is slow and irreversible, and a person reads the
plan in between. The merge itself is done by HDRMerge -- this orchestrates it.
"""

__version__ = "1.0.0"
