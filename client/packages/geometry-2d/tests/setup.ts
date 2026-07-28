/**
 * jsdom has no PointerEvent, and the editor is pointer-driven. A MouseEvent subclass
 * carrying `pointerId` is enough for the handlers under test.
 */
if (typeof globalThis.PointerEvent === 'undefined') {
  class PointerEventPolyfill extends MouseEvent {
    readonly pointerId: number;
    readonly pointerType: string;

    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 0;
      this.pointerType = init.pointerType ?? 'mouse';
    }
  }
  globalThis.PointerEvent = PointerEventPolyfill as unknown as typeof globalThis.PointerEvent;
}

// jsdom implements neither pointer capture nor SVGElement.focus().
if (typeof Element !== 'undefined') {
  Element.prototype.setPointerCapture ??= function setPointerCapture() {};
  Element.prototype.releasePointerCapture ??= function releasePointerCapture() {};
  if (typeof SVGElement !== 'undefined' && !SVGElement.prototype.focus) {
    SVGElement.prototype.focus = function focus() {};
  }
}
