#version 450

layout(location = 0) in vec4 in_color;
layout(location = 1) in vec2 in_texcoord;
layout(location = 0) out vec4 out_color;
layout(set = 0, binding = 1) uniform sampler2D base_color_texture;

void main() {
    out_color = in_color * texture(base_color_texture, vec2(in_texcoord.x, 1.0 - in_texcoord.y));
}
