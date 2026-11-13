"""Pure shared margin policy; a score is never a calibrated probability."""
import math


def candidate_ambiguity(manifest, scores):
    manifest = manifest if isinstance(manifest, dict) else {}
    threshold = manifest.get('margin_threshold')
    valid = (type(threshold) in (float, int) and math.isfinite(threshold) and threshold >= 0
        and all(isinstance(manifest.get(key), str) and manifest[key].strip()
            for key in ('threshold_version', 'calibration_evidence', 'score_kind', 'label_digest')))
    if valid and len(scores) >= 2 and all(type(score) in (float, int) and math.isfinite(score) for score in scores[:2]):
        margin = scores[0]-scores[1]
        if margin >= 0:
            return 'close' if margin <= threshold else 'separated'
    return 'unknown'
