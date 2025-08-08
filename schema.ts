export type LogsType = {
    machine_id?: string;
    logs: string;
    timestamp?: number;
}[];

// import type { CivitaiModelResponse } from "./types/civitai";

// import {
//     type DependencyGraphType,
// } from "comfyui-json";
import { desc, type InferSelectModel, relations, sql } from "drizzle-orm";
import {
    type AnyPgColumn,
    boolean,
    integer,
    jsonb,
    pgEnum,
    pgSchema,
    real,
    text,
    timestamp,
    uuid,
    index,
    unique,
    bigint,
    uniqueIndex,
} from "drizzle-orm/pg-core";
import { createInsertSchema, createSelectSchema } from "drizzle-zod";
import { z } from "zod";
export const dbSchema = pgSchema("comfyui_deploy");

export const usersTable = dbSchema.table("users", {
    id: text("id").primaryKey().notNull(),
    username: text("username").notNull(),
    name: text("name").notNull(),
    created_at: timestamp("created_at").defaultNow(),
    updated_at: timestamp("updated_at").defaultNow(),
});

export const workflowTable = dbSchema.table("workflows", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    org_id: text("org_id"),
    name: text("name").notNull(),
    selected_machine_id: uuid("selected_machine_id").references(
        () => machinesTable.id,
        {
            onDelete: "set null",
        },
    ),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    pinned: boolean("pinned").default(false).notNull(),
    deleted: boolean("deleted").default(false).notNull(),
    description: text("description"),
    cover_image: text("cover_image"),
}, (table) => ({
    idx_workflows_org_user: index("idx_workflows_org_user").on(table.org_id, table.user_id),
  }));

export const workflowSchema = createSelectSchema(workflowTable);

export const workflowRelations = relations(workflowTable, ({ many, one }) => ({
    user: one(usersTable, {
        fields: [workflowTable.user_id],
        references: [usersTable.id],
    }),
    versions: many(workflowVersionTable),
    deployments: many(deploymentsTable),
    selected_machine: one(machinesTable, {
        fields: [workflowTable.selected_machine_id],
        references: [machinesTable.id],
    }),
    machine: one(machinesTable, {
        fields: [workflowTable.id],
        references: [machinesTable.id],
    }),
    runs: many(workflowRunsTable),
}));

export const workflowType = z.any();
export const workflowTypeRaw = z.object({
    last_node_id: z.number(),
    last_link_id: z.number(),
    nodes: z.array(
        z.object({
            id: z.number(),
            type: z.string(),
            widgets_values: z.array(z.any()),
        })
    ),
});

export const workflowAPINodeType = z.object({
    inputs: z.record(z.any()),
    class_type: z.string().optional(),
});

export const workflowAPIType = z.record(workflowAPINodeType);

export const workflowVersionTable = dbSchema.table("workflow_versions", {
    workflow_id: uuid("workflow_id")
        .notNull()
        .references(() => workflowTable.id, {
            onDelete: "cascade",
        }),
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    workflow: jsonb("workflow").$type<z.infer<typeof workflowType>>(),
    workflow_api: jsonb("workflow_api").$type<z.infer<typeof workflowAPIType>>(),
    user_id: text("user_id").references(() => usersTable.id, {
        onDelete: "set null",
    }),
    comment: text("comment"),
    version: integer("version").notNull(),
    snapshot: jsonb("snapshot").$type<z.infer<typeof snapshotType>>(),
    dependencies:
        jsonb("dependencies"),//.$type<z.infer<typeof DependencyGraphType>>(),

    machine_version_id: uuid("machine_version_id"),
    machine_id: uuid("machine_id"),

    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    comfyui_snapshot: jsonb("comfyui_snapshot").$type<z.infer<typeof snapshotType>>(),
}, (table) => {
    return {
        idx_workflow_version_workflow_id: index(
            "idx_workflow_version_workflow_id",
        ).on(table.workflow_id),
    };
},);

export const CustomNodesDepsType = z.record(
    z.object({
        name: z.string(),
        node: z.array(workflowAPINodeType).optional(),
        hash: z.string().optional(),
        url: z.string(),
        files: z.array(z.string()).optional(),
        install_type: z
            .union([z.enum(["copy", "unzip", "git-clone"]), z.string()])
            .optional(),
        warning: z.string().optional(),
        pip: z.array(z.string()).optional(),
    }),
);

// export const CustomNodesDepsType = z.record(
//   z.object({
//     name: z.string(),
//     node: z.array(workflowAPINodeType).optional(),
//     hash: z.string().optional(),
//     url: z.string(),
//     pip: z.array(z.string()).optional(),
//     warning: z.string().optional(),
//   }),
// );

export const FileReferenceType = z.object({
    name: z.string(),
    hash: z.string().optional(),
    url: z.string().optional(),
});

export const FileReferencesType = z.record(z.array(FileReferenceType));

// export const DependencyGraphType = z.object({
//   comfyui: z.string(),
//   missing_nodes: z.array(z.string()).optional(),
//   custom_nodes: CustomNodesDepsType, // Replace z.any() with the specific type if known
//   models: FileReferencesType, // Replace z.any() with the specific type if known
//   files: FileReferencesType, // Replace z.any() with the specific type if known
// });

export const workflowVersionSchema = createSelectSchema(workflowVersionTable);

export const workflowVersionRelations = relations(
    workflowVersionTable,
    ({ one }) => ({
        workflow: one(workflowTable, {
            fields: [workflowVersionTable.workflow_id],
            references: [workflowTable.id],
        }),
        user: one(usersTable, {
            fields: [workflowVersionTable.user_id],
            references: [usersTable.id],
        }),
    }),
);

export const workflowRunStatus = pgEnum("workflow_run_status", [
    "not-started",
    "running",
    "uploading",
    "success",
    "failed",
    "started",
    "queued",
    "timeout",
    "cancelled",
]);

export const webhook_status = pgEnum("webhook_status", [
    "success",
    "failed",
    "not-started",
    "running",
]);

export const deploymentEnvironment = pgEnum("deployment_environment", [
    "staging",
    "preview",
    "production",
    "public-share",
    "private-share",
    "community-share",
]);

export const workflowRunOrigin = pgEnum("workflow_run_origin", [
    "manual",
    "api",
    "public-share",
    "public-template",
    "workspace",
]);

export const WorkflowRunStatusSchema = z.enum(workflowRunStatus.enumValues);

export const WorkflowRunOriginSchema = z.enum(workflowRunOrigin.enumValues);
export type WorkflowRunOriginType = z.infer<typeof WorkflowRunOriginSchema>;

export const machineGPUOptions = ["CPU", "T4", "L4", "A10G", "L40S", "A100", "A100-80GB", "H100", "H200", "B200"] as const;
export const machineGPUOptionsTypes = pgEnum("machine_gpu", machineGPUOptions);
export type machineGPUOptionsTypes = (typeof machineGPUOptions)[number];

export const workspace_machineGPUOptions = ["4090"] as const;
export const workspace_machineGPUOptionsTypes = pgEnum(
    "workspace_machine_gpu",
    workspace_machineGPUOptions,
);
export type workspace_machineGPUOptionsType =
    (typeof workspace_machineGPUOptions)[number];

export const machinesType = pgEnum("machine_type", [
    "classic",
    "runpod-serverless",
    "modal-serverless",
    "comfy-deploy-serverless",
    "workspace",
    "workspace-v2"
]);

export const machineStatusTypes = [
    "not-started",
    "ready",
    "building",
    "error",
    "running",
    "paused",
    "starting",
] as const;

export const machinesStatus = pgEnum("machine_status", machineStatusTypes);

export const machineBuilderVersionTypes = ["2", "3", "4"] as const;
export const machineBuilderVersion = pgEnum(
    "machine_builder_version",
    machineBuilderVersionTypes,
);

// We still want to keep the workflow run record.
export const workflowRunsTable = dbSchema.table(
    "workflow_runs",
    {
        id: uuid("id").primaryKey().defaultRandom().notNull(),
        // when workflow version deleted, still want to keep this record
        workflow_version_id: uuid("workflow_version_id").references(
            () => workflowVersionTable.id,
            {
                onDelete: "set null",
            },
        ),
        workflow_inputs:
            jsonb("workflow_inputs").$type<Record<string, string | number>>(),
        workflow_id: uuid("workflow_id")
            .references(() => workflowTable.id, {
                onDelete: "cascade",
            }),
        // Use for overriding the workflow run
        workflow_api: jsonb("workflow_api").$type<z.infer<typeof workflowAPIType>>(),
        // when machine deleted, still want to keep this record
        machine_id: uuid("machine_id").references(() => machinesTable.id, {
            onDelete: "set null",
        }),
        origin: workflowRunOrigin("origin").notNull().default("api"),
        status: workflowRunStatus("status").notNull().default("not-started"),
        ended_at: timestamp("ended_at"),
        // comfy deploy run created time
        created_at: timestamp("created_at").defaultNow().notNull(),
        // last time that the run was updated
        updated_at: timestamp("updated_at").defaultNow().notNull(),
        // modal gpu cold start begin
        queued_at: timestamp("queued_at"),
        // modal gpu function actual start time
        started_at: timestamp("started_at"),
        gpu_event_id: text("gpu_event_id"),
        gpu: machineGPUOptionsTypes("gpu"),
        machine_version: text("machine_version"),
        machine_type: machinesType("machine_type"),
        modal_function_call_id: text("modal_function_call_id"),
        user_id: text("user_id"),
        org_id: text("org_id"),
        run_log: jsonb("run_log").$type<LogsType>(),
        live_status: text("live_status"),
        progress: real("progress").default(0).notNull(),
        is_realtime: boolean("is_realtime").default(false).notNull(),
        webhook: text("webhook"),
        webhook_status: webhook_status("webhook_status"),
        webhook_intermediate_status: boolean("webhook_intermediate_status").default(false).notNull(),
        
        batch_id: uuid("batch_id"),
        favorite: boolean("favorite").default(false).notNull(),
        model_id: text("model_id"),

        deployment_id: uuid("deployment_id"),
    },
    (table) => {
        return {
            idx_workflow_runs_workflow_id: index("idx_workflow_runs_workflow_id").on(
                table.workflow_id,
            ),

            idx_workflow_run_created_at_desc: index("idx_workflow_run_created_at_desc").on(
                table.workflow_id,
                desc(table.created_at),
            ),

            // Index for optimizing queue position queries
            idx_workflow_run_queue_position: index("idx_workflow_run_queue_position").on(
                table.machine_id,
                table.status,
                table.created_at
            ),

            // Index for optimizing deployment_id queries with common filters
            idx_workflow_run_deployment: index("idx_workflow_run_deployment").on(
                table.deployment_id,
                table.status,
                desc(table.created_at)
            ),
        };
    },
);

export const workflowRunRelations = relations(
    workflowRunsTable,
    ({ one, many }) => ({
        machine: one(machinesTable, {
            fields: [workflowRunsTable.machine_id],
            references: [machinesTable.id],
        }),
        version: one(workflowVersionTable, {
            fields: [workflowRunsTable.workflow_version_id],
            references: [workflowVersionTable.id],
        }),
        outputs: many(workflowRunOutputs),
        workflow: one(workflowTable, {
            fields: [workflowRunsTable.workflow_id],
            references: [workflowTable.id],
        }),
    }),
);

// We still want to keep the workflow run record.
export const workflowRunOutputs = dbSchema.table(
    "workflow_run_outputs",
    {
        id: uuid("id").primaryKey().defaultRandom().notNull(),
        run_id: uuid("run_id")
            .notNull()
            .references(() => workflowRunsTable.id, {
                onDelete: "cascade",
            }),
        data: jsonb("data").$type<any>(),
        node_meta: jsonb("node_meta").$type<any>(),

        created_at: timestamp("created_at").defaultNow().notNull(),
        updated_at: timestamp("updated_at").defaultNow().notNull(),
    },
    (table) => {
        return {
            idx_workflow_run_outputs_run_id: index(
                "idx_workflow_run_outputs_run_id",
            ).on(table.run_id),
        };
    },
);
export const workflowOutputRelations = relations(
    workflowRunOutputs,
    ({ one }) => ({
        run: one(workflowRunsTable, {
            fields: [workflowRunOutputs.run_id],
            references: [workflowRunsTable.id],
        }),
    }),
);

export const ExtraDockerCommandsType = z.array(
    z.object({
        commands: z.array(z.string()),
        when: z.union([z.literal("before"), z.literal("after")]),
    }),
);

// share between machine versions and machines table
const machineColumns = () => ({
    comfyui_version: text("comfyui_version"),
    gpu: machineGPUOptionsTypes("gpu"),
    docker_command_steps: jsonb("docker_command_steps").$type<any>(),
    allow_concurrent_inputs: integer("allow_concurrent_inputs").default(1),
    concurrency_limit: integer("concurrency_limit").default(2),
    // CPU and Memory resource requests/limits for Modal
    cpu_request: real("cpu_request"),
    cpu_limit: real("cpu_limit"),
    memory_request: integer("memory_request"),
    memory_limit: integer("memory_limit"),
    install_custom_node_with_gpu: boolean("install_custom_node_with_gpu").default(false),
    run_timeout: integer("run_timeout").default(60 * 5).notNull(),
    idle_timeout: integer("idle_timeout").default(60).notNull(),
    extra_docker_commands: jsonb("extra_docker_commands").$type<any>(),
    machine_builder_version: machineBuilderVersion("machine_builder_version").default("2"),
    base_docker_image: text("base_docker_image"),
    python_version: text("python_version"),
    extra_args: text("extra_args"),
    disable_metadata: boolean("disable_metadata").default(true),
    prestart_command: text("prestart_command"),
    keep_warm: integer("keep_warm").default(0).notNull(),

    status: machinesStatus("status").notNull().default("ready"),
    build_log: text("build_log"),
    machine_hash: text("machine_hash"),
});

export const machinesTable = dbSchema.table(
    "machines",
    {
        id: uuid("id").primaryKey().defaultRandom().notNull(),
        user_id: text("user_id")
            .references(() => usersTable.id, {
                onDelete: "cascade",
            })
            .notNull(),
        name: text("name").notNull(),
        org_id: text("org_id"),
        endpoint: text("endpoint").notNull(),
        created_at: timestamp("created_at").defaultNow().notNull(),
        updated_at: timestamp("updated_at").defaultNow().notNull(),
        disabled: boolean("disabled").default(false).notNull(),
        auth_token: text("auth_token"),
        type: machinesType("type").notNull().default("classic"),
        static_assets_status: machinesStatus("static_assets_status").notNull().default("not-started"),
        machine_version: text("machine_version"),
        snapshot: jsonb("snapshot").$type<any>(),
        models: jsonb("models").$type<any>(),
        ws_gpu: workspace_machineGPUOptionsTypes("ws_gpu"),
        pod_id: text("pod_id"),
        legacy_mode: boolean("legacy_mode").default(false).notNull(),
        ws_timeout: integer("ws_timeout").default(2),
        build_machine_instance_id: text("build_machine_instance_id"),
        modal_app_id: text("modal_app_id"),
        target_workflow_id: uuid("target_workflow_id").references(
            (): AnyPgColumn => workflowTable.id,
            {
                onDelete: "set null",
            },
        ),
        dependencies: jsonb("dependencies").$type<any>(),
        deleted: boolean("deleted").default(false).notNull(),
        allow_background_volume_commits: boolean("allow_background_volume_commits").default(false).notNull(),
        gpu_workspace: boolean("gpu_workspace").default(false).notNull(),
        retrieve_static_assets: boolean("retrieve_static_assets").default(false),
        object_info: jsonb("object_info").$type<any>(),
        object_info_str: text('object_info_str').$type<any>(),
        filename_list_cache: jsonb("filename_list_cache").$type<any>(),
        extensions: jsonb("extensions").$type<any>(),
        import_failed_logs: text("import_failed_logs"),
        machine_version_id: uuid("machine_version_id"),
        is_workspace: boolean("is_workspace").default(false).notNull(),
        optimized_runner: boolean("optimized_runner").default(false).notNull(),
        ...machineColumns(),
    },
    (table) => {
        return {
            // We might have to handle case with user_id and deleted
            idx_machines_org_id_deleted: index("idx_machines_org_id_deleted").on(
                table.org_id,
                table.deleted,
            ),
        };
    },
);

export const machineVersionsTable = dbSchema.table("machine_versions", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    machine_id: uuid("machine_id")
        .notNull()
        .references(() => machinesTable.id, {
            onDelete: "cascade",
        }),
    version: integer("version").notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    modal_image_id: text("modal_image_id"),
    ...machineColumns(),
}, (table) => {
    return {
        idx_machine_versions_machine_id: index("idx_machine_versions_machine_id").on(
            table.machine_id,
        ),
    };
});

export const secretsTable = dbSchema.table("secrets", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    org_id: text("org_id"),
    name: text("name").notNull(),
    environment_variables:
        jsonb("environment_variables").$type<z.infer<typeof environmentVariables>>(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
});

export const machineSecretsTable = dbSchema.table("machine_secrets", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    machine_id: uuid("machine_id")
        .notNull()
        .references(() => machinesTable.id, {
            onDelete: "cascade",
        }),
    secret_id: uuid("secret_id")
        .notNull()
        .references(() => secretsTable.id, {
            onDelete: "cascade", 
        }),
    created_at: timestamp("created_at").defaultNow().notNull(),
}, (table) => {
    return {
        idx_machine_secrets_machine_id: index("idx_machine_secrets_machine_id").on(
            table.machine_id,
        ),
        idx_machine_secrets_secret_id: index("idx_machine_secrets_secret_id").on(
            table.secret_id,
        ),
        unq_machine_secret: unique("unq_machine_secret").on(
            table.machine_id,
            table.secret_id,
        ),
    };
});

export const machinesRelations = relations(machinesTable, ({ one }) => ({
    target_workflow: one(workflowTable, {
        fields: [machinesTable.target_workflow_id],
        references: [workflowTable.id],
    }),
    current_version: one(machineVersionsTable, {
        fields: [machinesTable.machine_version_id],
        references: [machineVersionsTable.id],
    }),
}));

export const machineVersionsRelations = relations(machineVersionsTable, ({ one }) => ({
    machine: one(machinesTable, {
        fields: [machineVersionsTable.machine_id],
        references: [machinesTable.id],
    }),
}));

export const snapshotType = z.object({
    comfyui: z.string(),
    git_custom_nodes: z.record(
        z.object({
            hash: z.string(),
            disabled: z.boolean(),
        }),
    ),
    file_custom_nodes: z.array(z.any()),
});

export const insertMachineSchema = createInsertSchema(machinesTable, {
    // name: (schema) => schema.name.default("My Machine"),
    // endpoint: (schema) => schema.endpoint.default("http://127.0.0.1:8188"),
    // type: (schema) => schema.type.default("classic"),
    // auth_token: (schema) => schema.auth_token.default("").describe("Auth token"),
});

export const showcaseMedia = z.array(
    z.object({
        url: z.string().url(),
        isCover: z.boolean().default(false),
    }),
).default([]);

export const OutputFileType = z.object({
    url: z.string(),
    filename: z.string(),
});


const runOutputs = z.array(
    z.object({
        data: z.object({
            images: z.array(OutputFileType).optional(),
            files: z.array(OutputFileType).optional(),
            gifs: z.array(OutputFileType).optional(),
            text: z.array(z.string()).optional(),
        }),
    }),
);

export const WebookRequestBody = z.object({
    status: WorkflowRunStatusSchema,
    live_status: z.string(),
    progress: z.number(),
    run_id: z.string(),
    outputs: runOutputs,
});

export const share_options = z.object({
    allowClone: z.boolean().default(true).optional(),
    showComfyUI: z.boolean().default(true).optional().describe("Show ComfyUI"),
})

export const showcaseMediaNullable = z
    .array(
        z.object({
            url: z.string(),
            isCover: z.boolean().default(false),
        }),
    )
    .nullable();

export const environmentVariables = z
    .array(
        z.object({
            key: z.string(),
            encryptedValue: z.string(),
        }),
    )

export const deploymentsTable = dbSchema.table("deployments", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    org_id: text("org_id"),
    workflow_version_id: uuid("workflow_version_id")
        .notNull()
        .references(() => workflowVersionTable.id),
    workflow_id: uuid("workflow_id")
        .notNull()
        .references(() => workflowTable.id, {
            onDelete: "cascade",
        }),
    machine_id: uuid("machine_id")
        .notNull()
        .references(() => machinesTable.id),
    share_slug: text("share_slug"),
    description: text("description"),
    share_options:
        jsonb("share_options").$type<z.infer<typeof share_options>>(),
    showcase_media:
        jsonb("showcase_media").$type<z.infer<typeof showcaseMedia>>(),
    environment: deploymentEnvironment("environment").notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    featured: boolean("featured").default(false).notNull(),

    // V2 deployment system
    machine_version_id: uuid("machine_version_id"),
    concurrency_limit: integer("concurrency_limit").default(2).notNull(),
    modal_image_id: text("modal_image_id"),
    gpu: machineGPUOptionsTypes("gpu"),
    run_timeout: integer("run_timeout").default(60 * 5).notNull(),
    idle_timeout: integer("idle_timeout").default(0).notNull(),
    keep_warm: integer("keep_warm").default(0).notNull(),
    activated_at: timestamp("activated_at"),
    modal_app_id: text("modal_app_id"),
},
    (table) => {
        return {
            userSlugUnique: uniqueIndex("deployments_user_slug_unique")
                .on(table.user_id, table.share_slug)
                .where(sql`${table.org_id} IS NULL`),
            orgSlugUnique: uniqueIndex("deployments_org_slug_unique")
                .on(table.org_id, table.share_slug)
                .where(sql`${table.org_id} IS NOT NULL`),
            updatedAtIndex: index("deployments_updated_at_index").on(table.updated_at),
            modalImageIdIndex: index("deployments_modal_image_id_index").on(table.modal_image_id),
            userIdIndex: index("deployments_user_id_index").on(table.user_id),
            orgIdIndex: index("deployments_org_id_index").on(table.org_id),
            environmentSlugIndex: index("deployments_environment_slug_index").on(table.environment, table.share_slug),
        };
    }

);

export const shareSettings = createSelectSchema(deploymentsTable, {
    // id: z.string(),
    share_options: share_options.partial().describe("Options")
}).pick({
    id: true,
    showcase_media: true,
    description: true,
    share_options: true,
});

export const publicShareDeployment = z.object({
    id: z.string(),
    description: z.string().nullable(),
    showcase_media: showcaseMedia.describe("Showcase media").optional(),
});

// createInsertSchema(deploymentsTable, {
//   description: (schema) => schema.description.default(""),
//   showcase_media: () => showcaseMedia.default([]),
// }).pick({
//   description: true,
//   showcase_media: true,
// });

export const deploymentsRelations = relations(deploymentsTable, ({ one }) => ({
    machine: one(machinesTable, {
        fields: [deploymentsTable.machine_id],
        references: [machinesTable.id],
    }),
    version: one(workflowVersionTable, {
        fields: [deploymentsTable.workflow_version_id],
        references: [workflowVersionTable.id],
    }),
    workflow: one(workflowTable, {
        fields: [deploymentsTable.workflow_id],
        references: [workflowTable.id],
    }),
    user: one(usersTable, {
        fields: [deploymentsTable.user_id],
        references: [usersTable.id],
    }),
}));

export const sharedWorkflowsTable = dbSchema.table("shared_workflows", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id").references(() => usersTable.id, {
        onDelete: "cascade",
    }).notNull(),
    org_id: text("org_id"),
    workflow_id: uuid("workflow_id").references(() => workflowTable.id, {
        onDelete: "cascade",
    }).notNull(),
    workflow_version_id: uuid("workflow_version_id").references(() => workflowVersionTable.id),
    workflow_export: jsonb("workflow_export").$type<any>().notNull(),
    share_slug: text("share_slug").notNull().unique(),
    title: text("title").notNull(),
    description: text("description"),
    cover_image: text("cover_image"),
    is_public: boolean("is_public").default(true).notNull(),
    view_count: integer("view_count").default(0).notNull(),
    download_count: integer("download_count").default(0).notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
});

export const sharedWorkflowsRelations = relations(sharedWorkflowsTable, ({ one }) => ({
    workflow: one(workflowTable, {
        fields: [sharedWorkflowsTable.workflow_id],
        references: [workflowTable.id],
    }),
    workflow_version: one(workflowVersionTable, {
        fields: [sharedWorkflowsTable.workflow_version_id],
        references: [workflowVersionTable.id],
    }),
    user: one(usersTable, {
        fields: [sharedWorkflowsTable.user_id],
        references: [usersTable.id],
    }),
}));

export const scopes = z.array(z.string());

export const apiKeyTokenType = pgEnum("api_key_token_type", ["user", "machine", "scoped"]);

export const apiKeyTable = dbSchema.table("api_keys", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    key: text("key").notNull().unique(),
    name: text("name").notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    org_id: text("org_id"),
    revoked: boolean("revoked").default(false).notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    scopes: jsonb("scopes").$type<z.infer<typeof scopes>>(),
    token_type: apiKeyTokenType("token_type").default("user").notNull(),
});

export const userUsageTable = dbSchema.table("user_usage", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    org_id: text("org_id"),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    usage_time: real("usage_time").default(0).notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    ended_at: timestamp("ended_at").defaultNow().notNull(),
});

export const authRequestsTable = dbSchema.table("auth_requests", {
    request_id: text("request_id").primaryKey().notNull(),
    user_id: text("user_id"),
    org_id: text("org_id"),
    api_hash: text("api_hash"),
    created_at: timestamp("created_at").defaultNow().notNull(),
    expired_date: timestamp("expired_date"),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
});

export const resourceUpload = pgEnum("resource_upload", [
    "started",
    "success",
    "failed",
    "cancelled",
]);

export const modelUploadType = pgEnum("model_upload_type", [
    "civitai",
    "download-url",
    "huggingface", // remove?
    "other",
]);

// https://www.answeroverflow.com/m/1125106227387584552
export const modelTypes = [
    "checkpoint",
    "lora",
    "embedding",
    "vae",
    "clip",
    "clip_vision",
    "configs",
    "controlnet",
    "upscale_models",
    "ipadapter",
    "gligen",
    "unet",
    "custom",
    "custom_node",
] as const;
export const modelType = pgEnum("model_type", modelTypes);
export type modelEnumType = (typeof modelTypes)[number];

export const modelTable = dbSchema.table("models", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id").references(() => usersTable.id, {}), // perhaps a "special" user_id for global models
    org_id: text("org_id"),
    description: text("description"),

    user_volume_id: uuid("user_volume_id")
        .notNull()
        .references(() => userVolume.id, {
            onDelete: "cascade",
        }),

    model_name: text("model_name"),
    folder_path: text("folder_path"), // folder-path in the volume
    target_symlink_path: text("target_symlink_path"),

    civitai_id: text("civitai_id"),
    civitai_version_id: text("civitai_version_id"),
    civitai_url: text("civitai_url"),
    civitai_download_url: text("civitai_download_url"),
    civitai_model_response: jsonb("civitai_model_response"),

    // for our own storage
    hf_url: text("hf_url"),
    s3_url: text("s3_url"),
    download_progress: integer("download_progress").default(0),

    user_url: text("client_url"),
    filehash_sha256: text("file_hash_sha256"),

    is_public: boolean("is_public").notNull().default(true),
    status: resourceUpload("status").notNull().default("started"),
    upload_machine_id: text("upload_machine_id"), // TODO: review if actually needed
    upload_type: modelUploadType("upload_type").notNull(),
    model_type: modelType("model_type").default("checkpoint"),
    error_log: text("error_log"),

    size: bigint("size", { mode: "number" }),

    deleted: boolean("deleted").default(false).notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
});

export const assetsTable = dbSchema.table("assets", {
    id: text("id").primaryKey().notNull(),
    user_id: text("user_id").references(() => usersTable.id, {}),
    org_id: text("org_id"),
    
    // Basic asset info
    name: text("name").notNull(),
    is_folder: boolean("is_folder").default(false).notNull(),
    
    // Path-based hierarchy
    path: text("path").notNull().default("/"),
    
    // File info (null for folders)
    file_size: bigint("file_size", { mode: "number" }),
    url: text("url"),
    mime_type: text("mime_type"),
    
    // Timestamps and soft delete
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    deleted: boolean("deleted").default(false).notNull(),
}, (table) => {
    return {
        // Index for path-based queries
        idx_path: index("idx_path").on(table.path),
        // Index for user-specific path queries
        idx_user_path: index("idx_user_path").on(table.user_id, table.path),
        // Index for org-specific path queries
        idx_org_path: index("idx_org_path").on(table.org_id, table.path),
        // Index for combined user/org path queries
        idx_deleted: index("idx_deleted").on(table.deleted, table.path),
        idx_user_org_path: index("idx_user_org_path").on(
            table.user_id,
            table.org_id,
            table.path
        ),
    };
});

export const userVolume = dbSchema.table("user_volume", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id").references(() => usersTable.id, {
        // onDelete: "cascade",
    }),
    org_id: text("org_id"),
    volume_name: text("volume_name").notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    disabled: boolean("disabled").default(false).notNull(),
});

export const modelRelations = relations(modelTable, ({ one }) => ({
    user: one(usersTable, {
        fields: [modelTable.user_id],
        references: [usersTable.id],
    }),
    volume: one(userVolume, {
        fields: [modelTable.user_volume_id],
        references: [userVolume.id],
    }),
}));

export const modalVolumeRelations = relations(userVolume, ({ many, one }) => ({
    model: many(modelTable),
    user: one(usersTable, {
        fields: [userVolume.user_id],
        references: [usersTable.id],
    }),
}));

const subscriptionPlanValues = [
    "basic",
    "pro",
    "enterprise",
    "creator",
    "business",
    "ws_basic",
    "ws_pro",
] as const;
export const subscriptionPlan = pgEnum("subscription_plan", subscriptionPlanValues);
export type SubscriptionPlanType = (typeof subscriptionPlanValues)[number];

export const subscriptionPlanStatus = pgEnum("subscription_plan_status", [
    "active",
    "deleted",
    "paused",
]);

export const subscriptionStatusTable = dbSchema.table("subscription_status", {
    stripe_customer_id: text("stripe_customer_id").primaryKey().notNull(),
    user_id: text("user_id"),
    org_id: text("org_id"),
    plan: subscriptionPlan("plan").notNull(),
    status: subscriptionPlanStatus("status").notNull(),
    subscription_id: text("subscription_id"),
    subscription_item_plan_id: text("subscription_item_plan_id"),
    subscription_item_api_id: text("subscription_item_api_id"), // not used in new version of billing
    cancel_at_period_end: boolean("cancel_at_period_end").default(false),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
    trial_end: integer("trial_end"),
    trial_start: integer("trial_start"),
    // last_invoice_date: timestamp("last_invoice_date").defaultNow().notNull(),
    last_invoice_timestamp: timestamp("last_invoice_timestamp").defaultNow().notNull(),
});

export const credits = dbSchema.table("credits", {
    user_or_org_id: text("user_or_org_id").primaryKey().notNull(),
    ws_credit: real("ws_credit").default(100).notNull(),
    last_updated: timestamp("last_updated").defaultNow().notNull(),
});

export const gpuProviders = ["modal", "runpod", "fal"] as const;
export const gpuProviderType = pgEnum("gpu_provider", gpuProviders);
export type gpuProviderType = (typeof gpuProviders)[number];

export const gpuEvents = dbSchema.table("gpu_events", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id)
        .notNull(),
    org_id: text("org_id"),
    machine_id: uuid("machine_id").references(() => machinesTable.id, {
        onDelete: "set null",
    }),
    start_time: timestamp("start_time"),
    end_time: timestamp("end_time"),
    gpu: machineGPUOptionsTypes("gpu"),
    ws_gpu: workspace_machineGPUOptionsTypes("ws_gpu"),
    providerType: gpuProviderType("gpu_provider").notNull(),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),

    // Used for direct billing
    cost_item_title: text("cost_item_title"),
    cost: real("cost").default(0),

    // Each gpu event can be associated with a session
    session_timeout: integer("session_timeout"),
    session_id: text("session_id"),
    modal_function_id: text("modal_function_id"),
    tunnel_url: text("tunnel_url"),
    machine_version_id: uuid("machine_version_id"),

    environment: deploymentEnvironment("environment"),
}, (table) => {
    return {
      session_id_idx: index("session_id_idx").on(table.session_id),
      end_time_idx: index("end_time_idx").on(table.end_time)
    }
}
);

const trainingTypeEnum = ["flux-lora"] as const;
export const trainingType = pgEnum("training_type", trainingTypeEnum);

export const trainings = dbSchema.table("trainings", {
    id: text("id").primaryKey().notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id)
        .notNull(),
    org_id: text("org_id"),

    name: text("name").notNull(),
    inputs: jsonb("inputs"),
    outputs: jsonb("outputs"),
    status: resourceUpload("status").notNull().default("started"),
    type: trainingType("type").notNull(),

    request_id: text("request_id"),

    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
});

export const formSubmissionsTable = dbSchema.table("form_submissions", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id").references(() => usersTable.id, {
        onDelete: "set null",
    }),
    org_id: text("org_id"),
    inputs: jsonb("inputs").$type<Record<string, any>>(),
    call_booked: boolean("call_booked").default(false).notNull(),
    discord_thread_id: text("discord_thread_id"),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),
});

export const outputVisibility = pgEnum("output_visibility", ["public", "private"]);

export const apiVersion = pgEnum("api_version", ["v1", "v2"]);

export const userSettingsTable = dbSchema.table("user_settings", {
    id: uuid("id").primaryKey().defaultRandom().notNull(),
    user_id: text("user_id")
        .references(() => usersTable.id, {
            onDelete: "cascade",
        })
        .notNull(),
    org_id: text("org_id"),
    // custom_domain: text("custom_domain"),
    output_visibility: outputVisibility("output_visibility").default("public"),
    custom_output_bucket: boolean("custom_output_bucket").default(false),
    s3_access_key_id: text("s3_access_key_id"),
    s3_secret_access_key: text("s3_secret_access_key"),
    assumed_role_arn: text("assumed_role_arn"),
    encrypted_s3_key: text("encrypted_s3_key"),
    s3_bucket_name: text("s3_bucket_name"),
    s3_region: text("s3_region"),
    use_cloudfront: boolean("use_cloudfront").default(false),
    cloudfront_domain: text("cloudfront_domain"),
    created_at: timestamp("created_at").defaultNow().notNull(),
    updated_at: timestamp("updated_at").defaultNow().notNull(),

    api_version: apiVersion("api_version").default("v2"),

    // for spend limit
    spend_limit: real("spend_limit").default(500.0).notNull(),
    max_spend_limit: real("max_spend_limit").default(1000).notNull(),

    hugging_face_token: text("hugging_face_token"),
    workflow_limit: real("workflow_limit"),
    machine_limit: real("machine_limit"),
    always_on_machine_limit: integer("always_on_machine_limit").default(0),

    credit: real("credit").default(0).notNull(),

    max_gpu: integer("max_gpu").default(0),
    enable_custom_output_bucket: boolean("enable_custom_output_bucket").default(false),
});

export type UserSettingsType = InferSelectModel<typeof userSettingsTable>;

export const updateUserSettingsSchema = createInsertSchema(userSettingsTable, {
    api_version: z.enum(["v1", "v2"]).default("v1").describe("Dashboard API Version"),
    output_visibility: z
        .enum(["public", "private"])
        .default("public")
        .describe("Output Visibility"),
    custom_output_bucket: z
        .boolean()
        .default(false)
        .optional()
        .describe("Enable Custom Output Bucket"),
    s3_access_key_id: z.string().optional().describe("S3 Access Key ID"),
    s3_secret_access_key: z.string().optional().describe("S3 Secret Access Key"),
    s3_bucket_name: z.string().optional().describe("S3 Bucket Name"),
    encrypted_s3_key: z.string().optional(),
    assumed_role_arn: z.string().optional().describe("Assumed Role ARN"),
    s3_region: z.string().optional().describe("S3 Region"),
    use_cloudfront: z.boolean().default(false).optional().describe("Use CloudFront CDN for custom bucket"),
    cloudfront_domain: z.string().optional().describe("CloudFront domain (e.g. dxxxxx.cloudfront.net)"),
    spend_limit: z.coerce
        .number()
        .default(5.0)
        .describe("Workspace budget (maximum usage per billing period)"),
    max_spend_limit: z.number().default(5).describe("Maximum spend limit"),
    hugging_face_token: z.string().optional().describe("Hugging Face Token"),
    workflow_limit: z.coerce.number().describe("Workflow Limit"),
    machine_limit: z.coerce.number().describe("Machine Limit"),
}).pick({
    api_version: true,
    output_visibility: true,
    custom_output_bucket: true,
    s3_access_key_id: true,
    s3_secret_access_key: true,
    s3_bucket_name: true,
    s3_region: true,
    use_cloudfront: true,
    cloudfront_domain: true,
    spend_limit: true,
    max_spend_limit: true,
    assumed_role_arn: true,
    hugging_face_token: true,
});

export type FormSubmissionType = InferSelectModel<typeof formSubmissionsTable>;

export type UserType = InferSelectModel<typeof usersTable>;
export type WorkflowType = InferSelectModel<typeof workflowTable>;
export type WorkflowRunType = InferSelectModel<typeof workflowRunsTable>;
export type WorkflowRunOutputType = InferSelectModel<typeof workflowRunOutputs>;
export type MachineType = InferSelectModel<typeof machinesTable>;
export type WorkflowVersionType = InferSelectModel<typeof workflowVersionTable>;
export type DeploymentType = InferSelectModel<typeof deploymentsTable>;
export type ModelType = InferSelectModel<typeof modelTable>;
export type UserVolumeType = InferSelectModel<typeof userVolume>;
export type UserUsageType = InferSelectModel<typeof userUsageTable>;
export type APIKeyType = InferSelectModel<typeof apiKeyTable>;
export type TrainingType = InferSelectModel<typeof trainings>;
export type SubscriptionStatusType = InferSelectModel<
    typeof subscriptionStatusTable
>;
export type GpuEventType = InferSelectModel<typeof gpuEvents>;
export type MachineVersionType = InferSelectModel<typeof machineVersionsTable>;
export type SharedWorkflowType = InferSelectModel<typeof sharedWorkflowsTable>;

export const outputShareVisibility = pgEnum("output_share_visibility", [
  "private",
  "public",
  "link"
]);

export const outputType = pgEnum("output_type", [
  "image",
  "video", 
  "3d",
  "other"
]);

export const outputSharesTable = dbSchema.table("output_shares", {
  id: uuid("id").primaryKey().defaultRandom().notNull(),
  user_id: text("user_id").references(() => usersTable.id).notNull(),
  org_id: text("org_id"),
  run_id: uuid("run_id").references(() => workflowRunsTable.id).notNull(),
  output_id: uuid("output_id").references(() => workflowRunOutputs.id).notNull(),
  deployment_id: uuid("deployment_id").references(() => deploymentsTable.id),
  output_data: jsonb("output_data").$type<any>(),
  inputs: jsonb("inputs").$type<any>(),
  output_type: outputType("output_type").notNull().default("other"),
  visibility: outputShareVisibility("visibility").notNull().default("private"),
  created_at: timestamp("created_at").defaultNow().notNull(),
  updated_at: timestamp("updated_at").defaultNow().notNull(),
}, (table) => {
  return {
    deploymentIdIndex: index("output_shares_deployment_id_index").on(table.deployment_id),
    userVisibilityCreatedIndex: index("output_shares_user_visibility_created_idx").on(table.user_id, table.visibility, table.created_at),
    visibilityCreatedIndex: index("output_shares_visibility_created_idx").on(table.visibility, table.created_at),
    typeCreatedIndex: index("output_shares_type_created_idx").on(table.output_type, table.created_at),
    deploymentCreatedIndex: index("output_shares_deployment_created_idx").on(table.deployment_id, table.created_at),
  };
});

export const outputSharesRelations = relations(outputSharesTable, ({ one }) => ({
  run: one(workflowRunsTable, {
    fields: [outputSharesTable.run_id],
    references: [workflowRunsTable.id],
  }),
  user: one(usersTable, {
    fields: [outputSharesTable.user_id],
    references: [usersTable.id],
  }),
  output: one(workflowRunOutputs, {
    fields: [outputSharesTable.output_id],
    references: [workflowRunOutputs.id],
  }),
  deployment: one(deploymentsTable, {
    fields: [outputSharesTable.deployment_id],
    references: [deploymentsTable.id],
  }),
}));

export type OutputShareType = InferSelectModel<typeof outputSharesTable>;
