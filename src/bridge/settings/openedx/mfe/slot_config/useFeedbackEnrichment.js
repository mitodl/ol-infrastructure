import { useRef } from 'react';
import { useModel } from './src/generic/model-store';

/**
 * Returns a STABLE getter for the enrichment fields merged into the feedback
 * POST (course name, unit title, current page URL). The getter reads a ref that
 * is refreshed every render, so values stay current across unit navigation
 * without re-initializing the bundle.
 */
export default function useFeedbackEnrichment(courseId, unitId) {
  const course = useModel('courseHomeMeta', courseId);
  const unit = useModel('units', unitId);

  const ref = useRef({});
  ref.current = {
    courseName: course?.title ?? '',
    unitTitle: unit?.title ?? '',
    url: window.location.href,
  };

  const getterRef = useRef(() => ref.current);
  return getterRef.current;
}
