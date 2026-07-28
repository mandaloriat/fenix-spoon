export { GeometryEditorElement, defineGeometryEditor, type EditorMode } from './element.js';
export { sampleClosedSpline, toPathData, type Point } from './spline.js';

import { defineGeometryEditor } from './element.js';

// Importing the package registers the element, which is what a plain
// `<script type="module">` consumer expects. Import from './element.js' directly
// to control registration (a different tag name, or none at all).
if (typeof customElements !== 'undefined') defineGeometryEditor();

declare global {
  interface HTMLElementTagNameMap {
    'fs-geometry-2d': import('./element.js').GeometryEditorElement;
  }
}
