{
  "ai": {
    "providers": {
      "openai": {
        "enabled": false,
        "api_key": "",
        "api_url": "https://api.openai.com/v1/responses",
        "models": [
          "gpt-5-nano",
          "gpt-5",
          "gpt-5-mini",
          "gpt-4o-search-preview",
          "gpt-4o-mini-search-preview"
        ]
      },
      "perplexity": {
        "enabled": false,
        "api_key": "",
        "api_url": "https://api.perplexity.ai/chat/completions",
        "models": ["sonar", "sonar-pro"]
      }
    },
    "groups": {
      "a_group": {
        "model": "gpt-5-nano",
        "prompts": [
          "short_description",
          "seo_keywords",
          "project_categories",
          "seo_short",
          "finalize",
          "connection_verification"
        ]
      },
      "b_group": {
        "model": "sonar-pro",
        "prompts": [],
        "web_search_options": {
          "search_context_size": "high"
        }
      },
      "c_group": {
        "model": "gpt-4o-search-preview",
        "api_url": "https://api.openai.com/v1/chat/completions",
        "prompts": [],
        "web_search_options": {
          "search_context_size": "high"
        }
      },
      "d_group": {
        "model": "gpt-5-mini",
        "prompts": ["review_full", "connection"],
        "web_search_options": {
          "search_context_size": "high"
        }
      }
    },
    "short_desc": {
      "max_len": 130,
      "retry_len": 100,
      "strapi_limit": 160
    },
    "seo_short": {
      "max_len": 50,
      "retry_len": 40,
      "strapi_limit": 60
    }
  },
  "apps": [
    {
      "app": "celestia",
      "enabled": true,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "0g",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "avail",
      "enabled": false,
      "categories": [
        "Chain",
        "Integrations",
        "Chains",
        "SDKs & Frameworks",
        "Gaming",
        "DeFi",
        "RaaS",
        "Bridge",
        "Wallets",
        "Infrastructure",
        "Education"
      ],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "babylon",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "berachain",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "canton",
      "enabled": false,
      "categories": [
        "Apps Users",
        "Featured Apps",
        "Governance",
        "Market Infrastructure",
        "Validators",
        "Service Provider",
        "Financial Institutions",
        "Industry Bodies"
      ],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "monad",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "movement",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "solana",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "h",
      "api_token": ""
    },
    {
      "app": "story",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "sui",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    },
    {
      "app": "supra",
      "enabled": false,
      "categories": [],
      "api_url_proj": "",
      "api_url_cat": "",
      "api_token": ""
    }
  ],
  "link_collections": ["linktr.ee", "link3.to", "bento.me", "hub.xyz"],
  "http": {
    "strategy": "random",
    "ua": [
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ]
  },
  "nitter": {
    "enabled": true,
    "instances": [
      "https://nitter.net",
      "https://xcancel.com",
      "https://nuku.trabun.org",
      "https://nitter.tiekoetter.com",
      "https://nitter.space",
      "https://lightbrd.com",
      "https://nitter.privacyredirect.com",
      "https://nitter.kuuro.net",
      "https://nitter.poast.org/"
    ],
    "timeout": 15,
    "bad_ttl": 600,
    "max_ins": 3,
    "strategy": "random"
  },
  "clear_logs": true,
  "strapi": {
    "strapi_sync": true,
    "strapi_publish": true,
    "http_timeout_sec": 45,
    "http_retries": 3,
    "http_backoff": 1.7
  },
  "coingecko": {
    "api_base": "https://api.coingecko.com/api/v3"
  },
  "bad_name_keywords": [
    "",
    "x",
    "profile",
    "new to x",
    "is live",
    "live",
    "launch",
    "update",
    "official",
    "home",
    "beta",
    "new",
    "promo",
    "airdrop",
    "listing",
    "sale",
    "event",
    "dashboard",
    "token",
    "the",
    "app",
    "site",
    "portal",
    "not found",
    "404",
    "error",
    "page-not-found",
    "notfound",
    "page error",
    "oops",
    "missing",
    "Introduction",
    "Home",
    "Docs"
  ],
  "categories": [
    "Multichain",
    "Crosschain",
    "dApp",
    "Modular",
    "Tools",
    "AI",
    "Infra",
    "DePIN",
    "Data",
    "Cloud",
    "RWAFi",
    "L1",
    "L2",
    "Rollup",
    "Wallet",
    "Edu",
    "SocialFi",
    "DeFi",
    "CeFi",
    "GameFi",
    "Staking",
    "Analytics",
    "NodeFi",
    "Bridge",
    "Quest",
    "ZK",
    "VM",
    "Marketplace",
    "IoT",
    "Identity",
    "Security",
    "Oracle",
    "AMM",
    "Trading",
    "NFT",
    "Metaverse",
    "Explorer",
    "DEX",
    "CEX",
    "Lending",
    "Meme",
    "DAO",
    "Chain",
    "Privacy",
    "Interoperability",
    "Invest",
    "DeFAI",
    "CeDeFi",
    "Launchpad",
    "Art",
    "Payment"
  ]
}
