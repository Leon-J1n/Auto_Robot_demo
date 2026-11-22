#version 450

layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec2 in_texcoord;
layout(location = 0) out vec4 out_color;
layout(location = 1) out vec2 out_texcoord;

const int MAX_LIGHTS = 8;

layout(set = 0, binding = 0) uniform SceneData {
    mat4 view_projection;
    vec4 light_position_type[MAX_LIGHTS];
    vec4 light_direction_range[MAX_LIGHTS];
    vec4 light_color_intensity[MAX_LIGHTS];
    vec4 light_params[MAX_LIGHTS];
    ivec4 light_meta;
} scene_data;

layout(push_constant) uniform PushConstants {
    mat4 model;
    vec4 color;
} push_data;

void main() {
    vec3 world_position = (push_data.model * vec4(in_position, 1.0)).xyz;
    gl_Position = scene_data.view_projection * vec4(world_position, 1.0);
    vec3 world_normal = normalize(mat3(push_data.model) * in_normal);
    vec3 illumination = vec3(0.0);
    for (int index = 0; index < scene_data.light_meta.x; ++index) {
        int type = int(scene_data.light_position_type[index].w + 0.5);
        if (type == 3) {
            illumination += scene_data.light_color_intensity[index].rgb *
                            scene_data.light_color_intensity[index].a;
            continue;
        }
        vec3 light_vector;
        float attenuation = 1.0;
        if (type == 2) {
            light_vector = normalize(-scene_data.light_direction_range[index].xyz);
        } else {
            vec3 delta = scene_data.light_position_type[index].xyz - world_position;
            float distance_to_light = length(delta);
            light_vector = distance_to_light > 0.00001 ? delta / distance_to_light : world_normal;
            float light_range = scene_data.light_direction_range[index].w;
            if (light_range > 0.0) {
                float normalized_distance = distance_to_light / light_range;
                attenuation = pow(max(1.0 - normalized_distance, 0.0),
                                  max(scene_data.light_params[index].z, 0.0001));
            }
            if (type == 1) {
                // Spot direction points from the source toward the illuminated object.
                float cone = dot(normalize(scene_data.light_direction_range[index].xyz), -light_vector);
                float inner_cone_cos = scene_data.light_params[index].x;
                float outer_cone_cos = scene_data.light_params[index].y;
                float cone_falloff = inner_cone_cos > outer_cone_cos + 0.00001
                    ? smoothstep(outer_cone_cos, inner_cone_cos, cone)
                    : step(outer_cone_cos, cone);
                attenuation *= cone_falloff;
            }
        }
        float diffuse = max(dot(world_normal, light_vector), 0.0);
        illumination += scene_data.light_color_intensity[index].rgb *
                        scene_data.light_color_intensity[index].a * attenuation * diffuse;
    }
    out_color = vec4(push_data.color.rgb * illumination, push_data.color.a);
    out_texcoord = in_texcoord;
}
