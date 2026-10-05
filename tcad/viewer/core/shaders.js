import CONTRACT from "./render-contract.json" with { type: "json" };
export const VERTEX = `
precision highp float;
attribute vec3 aPosition;
attribute vec3 aNormal;
uniform vec3 uTarget, uRight, uUp, uLook;
uniform float uHalfHeight, uAspect, uDepth;
varying vec3 vNormal;
void main() {
  vec3 p = aPosition - uTarget;
  gl_Position = vec4(dot(p,uRight)/(uHalfHeight*uAspect), dot(p,uUp)/uHalfHeight, -dot(p,uLook)/uDepth, 1.0);
  vNormal = aNormal;
}`;
export const FRAGMENT = `
precision highp float;
varying vec3 vNormal;
uniform vec3 uLook, uUp, uRight;
uniform bool uLines;
void main() {
  if (uLines) { gl_FragColor = vec4(vec3(${CONTRACT.edge.map(v => v / 255).join(",")}),1.0); return; }
  vec3 n = normalize(vNormal) * (gl_FrontFacing ? 1.0 : -1.0);
  vec3 light = normalize(uLook + ${CONTRACT.light.up}*uUp + ${CONTRACT.light.right}*uRight);
  float shade = ${CONTRACT.ambient} + ${CONTRACT.diffuse}*max(dot(n, light),0.0) + ${CONTRACT.rim}*max(dot(n,-uRight),0.0);
  gl_FragColor = vec4(vec3(${CONTRACT.color.join(",")})*shade, 1.0);
}`;
