export * from './colormap.js';
export * from './field.js';
export * from './streamlines.js';
export * from './viewport.js';
export {
  type OverlayContext,
  type ToolAvailability,
  type ToolbarTool,
  type ViewerCapabilities,
  type ViewerMode,
  FieldViewerElement,
  defineFieldViewer,
  geometryPolygons,
} from './element.js';

import { defineFieldViewer } from './element.js';

// Importing the package registers the element, which is what a plain
// `<script type="module">` consumer expects. Import the `@fenix-spoon/viewer/element`
// subpath instead to control registration (a different tag name, or none at all).
if (typeof customElements !== 'undefined') defineFieldViewer();

declare global {
  interface HTMLElementTagNameMap {
    'fs-viewer': import('./element.js').FieldViewerElement;
  }
}
